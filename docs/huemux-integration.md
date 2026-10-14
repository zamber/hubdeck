# HueMux integration

[HueMux](https://github.com/zamber/huemux) is a lightweight Hue frontend: a Go server (plus an
Electron desktop app and Android APK) that talks to the bridge directly and exposes a local WebSocket
plus a few HTTP endpoints. hubdeck has a `huemux` plugin (`src/hubdeck/plugins/huemux.py`) that
drives it, so keys, knobs, dials and faders control Philips Hue lights without touching the bridge's
CLIP API directly.

## The plugin

    plugins:
      huemux:
        url: http://127.0.0.1:7654       # HueMux's address, as in the browser

Actions: `toggle`, `turn_on`, `turn_off` (a light's id, or `room:<room id>`), `scene` (a scene id),
`favorite` (a light or room), `color` (`{light: <id>, rgb: "#ff8800"}`).
States: `connected`, `on[<id>]`, `brightness[<id>]` (light or `room:<room id>`).
Values (turn/fader targets): `brightness` (0-100), `color_temp` (0-100, 2000 K warm to 6500 K cool).

It fetches `/api/lights`, `/api/rooms` and `/api/scenes` over HTTP to list what exists and resolve
`room:` arguments, then lives on HueMux's `/ws` eventstream for on/brightness state.

## One gotcha: the Origin header

HueMux's `/ws` rejects a handshake with no `Origin` header — that is its defence against a hostile
web page driving the lights, and a browser always sends one. hubdeck's WebSocket client is not a
browser, so the plugin connects with `Origin: http://localhost` (accepted unconditionally). This
required a small, general change: `net.websocket.WebSocket` now takes an optional `headers` dict of
extra handshake headers.

## Other integration routes considered

1. **HueMux WS plugin (built).** Best fit: HueMux already owns the bridge pairing, the eventstream
   and the light-control surface, and hubdeck has a minimal WS client plus the plugin machinery.
2. **HueMux HTTP endpoints.** `GET /api/{lights,rooms,scenes}` are read-only snapshots; mutations
   are WS-only, so HTTP alone can't control lights — it's only the picker feed.
3. **Home Assistant plugin (existing).** If the lights are already in Home Assistant, the `ha`
   plugin already reaches them; HueMux is the lighter, direct-bridge alternative.
4. **Raw CLIP v2.** HueMux already handles pairing, TLS and the eventstream; reimplementing that in
   hubdeck would duplicate a whole product. Not worth it while HueMux exists.

## Running it on a hubdeck host

1. Run HueMux (paired with the bridge), default `http://127.0.0.1:7654`.
2. Point `plugins.huemux.url` at it, use `huemux.toggle`, `huemux.brightness`, `huemux.scene`, ... in
   a config, and `hubdeck run` (or the AppImage) will drive the lights through HueMux.
