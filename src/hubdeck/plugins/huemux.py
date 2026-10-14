"""HueMux: control Philips Hue lights and rooms through HueMux's local server.

plugins.huemux takes `url`, HueMux's address as in the browser (default http://127.0.0.1:7654).
Hubdeck talks to the same /ws WebSocket the Lights page uses, so keys can toggle, dim, recall
scenes and set colour without reaching the bridge's CLIP API directly. It fetches /api/lights,
/api/rooms and /api/scenes over HTTP to list what exists and to resolve `room:<id>` arguments,
then lives on the eventstream for on/brightness state.

HueMux (github.com/zamber/huemux) is a separate program that must be running and paired with the
bridge. Arguments are HueMux's own ids: a light's id, `room:<room id>`, or a scene's id.

HueMux's /ws rejects connections without a matching Origin header (it is how it stops a hostile
page from driving the lights), so this plugin connects with Origin: http://localhost. See
docs/huemux-integration.md.
"""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..net.websocket import WebSocket, WebSocketError
from .base import ThreadedPlugin, adjusted, state_key, value_state

TIMEOUT = 5               # seconds to connect or fetch
RETRY_SECONDS = 5
REFRESH_SECONDS = 60      # lights/rooms/scenes fetched again this often, for the picker
SETTINGS = ("url", "verify_ssl")
MIRED_WARM = 500          # 2000 K
MIRED_COOL = 153          # 6500 K
ROOM = "room:"            # prefix that addresses a room instead of a light


def room_arg(arg: Any) -> str | None:
    """'room:<id>' -> id, anything else -> None."""
    if isinstance(arg, str) and arg.startswith(ROOM) and len(arg) > len(ROOM):
        return arg[len(ROOM):]
    return None


def ws_url(url: str) -> str:
    """http(s)://host[:port][/path] -> ws(s)://host[:port][/path]/ws"""
    parts = urllib.parse.urlsplit(url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws", "", ""))


def to_mirek(value: float) -> int:
    """0-100 -> mirek: 0 is warm (2000 K), 100 is cool (6500 K)."""
    return round(MIRED_WARM - (MIRED_WARM - MIRED_COOL) * max(0.0, min(100.0, value)) / 100)


def from_mirek(mirek: int | None) -> float | None:
    if mirek is None:
        return None
    return round(max(0.0, min(100.0, (MIRED_WARM - mirek) / (MIRED_WARM - MIRED_COOL) * 100)))


def rgb_of(text: Any) -> tuple[int, int, int] | None:
    """'#rrggbb' or '#rgb' -> (r, g, b) each 0-255, or None."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", text) or re.fullmatch(r"#([0-9a-fA-F]{3})", text)
    if not m:
        return None
    hex6 = m[1] if len(m[1]) == 6 else "".join(c * 2 for c in m[1])
    return tuple(int(hex6[i:i + 2], 16) for i in (0, 2, 4))


class HueMuxPlugin(ThreadedPlugin):
    name = "huemux"
    actions = {
        "toggle": "toggle a light or room, e.g. {huemux.toggle: room:kitchen}",
        "turn_on": "turn a light or room on, e.g. {huemux.turn_on: <light id>}",
        "turn_off": "turn a light or room off, e.g. {huemux.turn_off: room:<room id>}",
        "scene": "recall a scene, e.g. {huemux.scene: <scene id>}",
        "favorite": "toggle a favourite, e.g. {huemux.favorite: <light id>}",
        "color": "set a light's colour, e.g. {huemux.color: {light: <id>, rgb: \"#ff8800\"}}",
    }
    states = {
        "connected": "whether HueMux is connected",
        "on": "whether a light or room is on: huemux.on[<light id>] or huemux.on[room:<room id>]",
        "brightness": "a light or room's brightness 0-100: huemux.brightness[<light id>]",
    }
    state_args = {"on": "a light's id, or room:<room id>", "brightness": "a light's id, or room:<room id>"}
    values = {
        "brightness": "a light or room's brightness 0-100 (arg: the light's id, or room:<room id>)",
        "color_temp": "a light's white point 0-100 (arg: the light's id); 0 = warm 2000 K, 100 = 6500 K",
    }
    fetches_states = True

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self._ws: WebSocket | None = None
        self._retry_at = 0.0
        self._last_error: str | None = None
        self._refresh_at = 0.0
        self._lights: dict[str, dict] = {}       # id -> {name, on, brightness, dimmable, colorable, mirek}
        self._rooms: dict[str, dict] = {}        # room id -> {name, grouped_light_id, on, brightness}
        self._by_grouped: dict[str, str] = {}    # grouped_light_id -> room id
        self._scenes: dict[str, str] = {}        # scene id -> name
        self._published: set[str] = set()

    # -- config checks ----------------------------------------------------
    @classmethod
    def check_config(cls, config):
        problems = []
        url = config.get("url", "http://127.0.0.1:7654")
        if not (isinstance(url, str) and re.fullmatch(r"https?://[^/\s]+(/\S*)?", url)):
            problems.append("plugins.huemux.url: HueMux's address as in the browser, e.g. http://127.0.0.1:7654")
        if not isinstance(config.get("verify_ssl", True), bool):
            problems.append("plugins.huemux.verify_ssl must be true or false")
        problems += [f"plugins.huemux: unknown setting {k!r} (available: {', '.join(SETTINGS)})"
                     for k in sorted(set(config) - set(SETTINGS))]
        return problems

    @classmethod
    def check_action(cls, action, arg):
        if action in ("toggle", "turn_on", "turn_off", "scene", "favorite"):
            if not (isinstance(arg, str) and arg.strip()):
                return f"huemux.{action} needs an id, e.g. a light's id, room:<room id>, or a scene's id"
            if arg.startswith(ROOM) and not arg[len(ROOM):].strip():
                return f"huemux.{action}[room:] needs the room's id after 'room:'"
        if action == "color":
            if not (isinstance(arg, dict) and isinstance(arg.get("light"), str) and arg["light"].strip()):
                return "huemux.color needs {light: <id>, rgb: \"#rrggbb\"}"
            if rgb_of(arg.get("rgb")) is None:
                return "huemux.color's rgb must be a colour like \"#ff8800\""
            if unknown := set(arg) - {"light", "rgb"}:
                return f"huemux.color: unknown field(s) {', '.join(sorted(map(str, unknown)))} (available: light, rgb)"
        return None

    @classmethod
    def check_state(cls, name, config, arg=None):
        if err := super().check_state(name, config, arg):
            return err
        if not (isinstance(arg, str) and arg.strip()):
            return f"huemux.{name} needs a light's id or room:<room id> in brackets"
        if arg.startswith(ROOM) and not arg[len(ROOM):].strip():
            return f"huemux.{name}[room:] needs the room's id after 'room:'"
        return None

    @classmethod
    def check_value(cls, name, arg):
        if not (isinstance(arg, str) and arg.strip()):
            return f"huemux.{name} needs a light's id or room:<room id> as its arg"
        if arg.startswith(ROOM) and not arg[len(ROOM):].strip():
            return f"huemux.{name}[room:] needs the room's id after 'room:'"
        return None

    # -- thread -------------------------------------------------------------
    def setup(self):
        self._set("connected", False)

    def teardown(self):
        if self._ws:
            self._ws.close()
            self._ws = None

    def tick(self):
        now = time.monotonic()
        try:
            if self._ws is None:
                if now < self._retry_at:
                    return
                self._connect(now)
                if self._ws is None:
                    return
            for msg in self._ws.pump():
                self._receive(msg)
            if now >= self._refresh_at:
                self._refresh_at = now + REFRESH_SECONDS
                self._fetch()
        except WebSocketError as e:
            self._drop(str(e), now)

    def _connect(self, now: float):
        try:
            ws = WebSocket(ws_url(self.config.get("url", "http://127.0.0.1:7654")), timeout=TIMEOUT,
                           verify_ssl=self.config.get("verify_ssl", True), peer="HueMux",
                           headers={"Origin": "http://localhost"})
        except WebSocketError as e:
            return self._failed(str(e), now)
        self._ws, self._last_error = ws, None
        self.log.info("connected to HueMux at %s", self.config.get("url", "http://127.0.0.1:7654"))
        self._set("connected", True)
        self._fetch()          # lights/rooms/scenes, for the picker and room: resolution

    def _failed(self, error: str, now: float):
        if error != self._last_error:
            self.log.warning("%s; retrying every %d s", error, RETRY_SECONDS)
            self._last_error = error
        self._retry_at = now + RETRY_SECONDS

    def _drop(self, error: str, now: float):
        if self._ws:
            self._ws.close()
        self._ws = None
        self._lights.clear()
        self._rooms.clear()
        self._by_grouped.clear()
        self._scenes.clear()
        for key in self._published - {"connected"}:
            self.ctx.set_state(key, None)
        self._set("connected", False)
        self._failed(error, now)

    def _send(self, msg: dict):
        if self._ws is None:
            self.log.warning("not connected to HueMux; dropping %s", msg.get("type"))
            return
        try:
            self._ws.send(msg)
        except WebSocketError as e:
            self._drop(str(e), time.monotonic())

    # -- HTTP ----------------------------------------------------------------
    def _http_json(self, path: str) -> Any:
        """GET path on HueMux's server; raises OSError / ValueError when it can't be read."""
        base = self.config.get("url", "http://127.0.0.1:7654").rstrip("/")
        context = None
        if base.startswith("https://") and not self.config.get("verify_ssl", True):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(base + path, timeout=TIMEOUT, context=context) as r:
            import json
            return json.loads(r.read().decode())

    def _fetch(self):
        try:
            lights = self._http_json("/api/lights")
            rooms = self._http_json("/api/rooms")
            scenes = self._http_json("/api/scenes")
        except (OSError, ValueError) as e:
            return self.log.warning("couldn't list HueMux lights: %s", e)
        self._apply(lights if isinstance(lights, list) else lights.get("lights", []),
                    rooms if isinstance(rooms, list) else rooms.get("rooms", []))
        self._scenes = {s["id"]: s.get("name", "") for s in (scenes if isinstance(scenes, list) else scenes.get("scenes", []))
                        if isinstance(s, dict) and s.get("id")}
        self._publish()

    def _apply(self, lights: list, rooms: list):
        self._lights = {l["id"]: {"name": l.get("name", ""), "on": bool(l.get("on")),
                                  "brightness": l.get("brightness"), "dimmable": bool(l.get("dimmable")),
                                  "colorable": bool(l.get("colorable")), "mirek": l.get("mirek")}
                        for l in lights if isinstance(l, dict) and l.get("id")}
        self._rooms = {r["id"]: {"name": r.get("name", ""), "grouped_light_id": r.get("grouped_light_id", ""),
                                 "on": bool(r.get("on")), "brightness": r.get("brightness")}
                       for r in rooms if isinstance(r, dict) and r.get("id")}
        self._by_grouped = {r["grouped_light_id"]: rid for rid, r in self._rooms.items() if r["grouped_light_id"]}

    # -- WS pushes -----------------------------------------------------------
    def _receive(self, msg: dict):
        kind = msg.get("type")
        if kind == "lights_snapshot":
            self._apply(msg.get("lights", []), msg.get("rooms", []))
            self._publish()
        elif kind == "light_event":
            self._changed(msg.get("event") or {})

    def _changed(self, event: dict):
        eid = event.get("id")
        if event.get("type") == "grouped_light" and eid in self._by_grouped:
            room = self._rooms[self._by_grouped[eid]]
            for field in ("on", "brightness"):
                if field in event:
                    room[field] = event[field]
        elif eid in self._lights:
            light = self._lights[eid]
            for field in ("on", "brightness", "mirek"):
                if field in event:
                    light[field] = event[field]
        self._publish()

    # -- state / value lookup ----------------------------------------------
    def _light_of(self, arg: str) -> dict | None:
        room = room_arg(arg)
        return self._rooms.get(room) if room is not None else self._lights.get(arg)

    def _read(self, name: str, arg: str) -> Any:
        target = self._light_of(arg)
        if target is None:
            return None
        if name == "on":
            return target.get("on")
        if name == "brightness":
            b = target.get("brightness")
            return None if b is None else round(b)
        if name == "color_temp":
            return from_mirek(target.get("mirek"))
        return None

    def _publish(self):
        for name, arg in self.watched_states:
            self._set(state_key(name, arg), self._read(name, arg))
        for name, arg in self.watched:
            self._set(value_state(name, arg), self._read(name, arg))

    def _set(self, key: str, value: Any):
        self.ctx.set_state(key, value)
        self._published.add(key)

    # -- actions and values -------------------------------------------------
    def _target_rid(self, arg: str) -> tuple[str, str]:
        """The (kind, resource id) Huemux's control messages want for `arg`."""
        room = room_arg(arg)
        if room is not None:
            grouped = self._rooms.get(room, {}).get("grouped_light_id")
            return "room", grouped or room
        return "light", arg

    def handle(self, action: str, arg: Any):
        if action in ("toggle", "turn_on", "turn_off"):
            kind, rid = self._target_rid(arg)
            if action == "turn_on":
                on = True
            elif action == "turn_off":
                on = False
            else:
                target = self._light_of(arg)
                on = not bool(target.get("on")) if target else True
            self._send({"type": ("room" if kind == "room" else "light") + "_toggle", "rid": rid, "on": on})
        elif action == "scene":
            self._send({"type": "scene_recall", "rid": arg})
        elif action == "favorite":
            self._send({"type": "light_favorite", "rid": arg})
        elif action == "color":
            r, g, b = rgb_of(arg["rgb"])
            self._send({"type": "light_color", "rid": arg["light"], "r": r, "g": g, "b": b})

    def adjust_value(self, name: str, arg: str, delta: float, expo: float):
        if name == "brightness":
            current = self._read("brightness", arg)
            self._set_level(arg, adjusted(0.0 if current is None else current, delta, expo))
        elif name == "color_temp":
            current = self._read("color_temp", arg)
            self._send({"type": "light_color_temp", "rid": arg,
                        "mirek": to_mirek(adjusted(50.0 if current is None else current, delta, expo))})

    def set_value(self, name: str, arg: str, value: float):
        if name == "brightness":
            self._set_level(arg, value)
        elif name == "color_temp":
            self._send({"type": "light_color_temp", "rid": arg, "mirek": to_mirek(value)})

    def _set_level(self, arg: str, level: float):
        kind, rid = self._target_rid(arg)
        self._send({"type": ("room" if kind == "room" else "light") + "_brightness",
                    "rid": rid, "brightness": max(0, round(max(0.0, min(100.0, level))))})
