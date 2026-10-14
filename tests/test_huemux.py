"""HueMux plugin against a fake HueMux: a WebSocket server plus stubbed HTTP endpoints."""
import pytest

from fakes.ws_server import FakeWsServer
from hubdeck.plugins.base import PluginContext
from hubdeck.plugins.huemux import HueMuxPlugin, from_mirek, rgb_of, to_mirek, ws_url
from test_engine import wait_for


LIGHTS = [
    {"id": "desk", "name": "Desk", "on": True, "brightness": 80.0, "dimmable": True,
     "colorable": True, "mirek": 300},
    {"id": "hall", "name": "Hall", "on": False, "brightness": 0.0, "dimmable": True, "colorable": False},
]
ROOMS = [
    {"id": "kitchen", "grouped_light_id": "grp-kitchen", "name": "Kitchen", "on": True, "brightness": 55.0},
]
SCENES = [{"id": "movie", "name": "Movie"}, {"id": "night", "name": "Night"}]


class FakeHueMux(FakeWsServer):
    """Records the control messages the plugin sends; sends a snapshot on connect."""

    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []

    def on_connect(self):
        self.send({"type": "lights_snapshot", "lights": LIGHTS, "rooms": ROOMS})

    def on_message(self, msg):
        self.sent.append(msg)


class Harness:
    WATCHED_STATES = {("on", "desk"), ("on", "hall"), ("on", "room:kitchen"),
                      ("brightness", "desk"), ("brightness", "room:kitchen")}
    WATCHED_VALUES = {("brightness", "desk"), ("brightness", "room:kitchen")}

    def __init__(self, fake, lights=LIGHTS, rooms=ROOMS, scenes=SCENES):
        self.fake = fake
        self.states = {}
        ctx = PluginContext("huemux", lambda plugin, key, value: self.states.__setitem__(key, value))
        self.plugin = HueMuxPlugin(ctx, {"url": f"http://127.0.0.1:{fake.port}"})
        payloads = {"lights": lights, "rooms": rooms, "scenes": scenes}
        self.plugin._http_json = lambda path: payloads[path.rsplit("/", 1)[1]]
        self.plugin.watch_states(set(self.WATCHED_STATES))
        self.plugin.watch(set(self.WATCHED_VALUES))

    def connect(self):
        self.plugin.start()
        wait_for(lambda: self.states.get("connected") is True)

    def expect(self, type_, **fields):
        """Wait until HueMux has received a control message of `type_` with the given fields."""
        wait_for(lambda: any(m.get("type") == type_ and all(m.get(k) == v for k, v in fields.items())
                             for m in self.fake.sent))

    def stop(self):
        self.plugin.stop()


@pytest.fixture
def hx():
    fake = FakeHueMux()
    h = Harness(fake)
    yield h
    h.stop()
    fake.close()


def test_ws_url():
    assert ws_url("http://127.0.0.1:7654") == "ws://127.0.0.1:7654/ws"
    assert ws_url("https://lights.lan/") == "wss://lights.lan/ws"
    assert ws_url("http://host:9000/base") == "ws://host:9000/base/ws"


def test_color_helpers():
    assert rgb_of("#ff8800") == (255, 136, 0)
    assert rgb_of("#f80") == (255, 136, 0)
    assert rgb_of("ff8800") is None
    assert rgb_of(None) is None
    assert to_mirek(0) == 500
    assert to_mirek(100) == 153
    assert from_mirek(500) == 0
    assert from_mirek(153) == 100
    assert from_mirek(None) is None


def test_check_config():
    assert HueMuxPlugin.check_config({"url": "http://127.0.0.1:7654"}) == []
    assert HueMuxPlugin.check_config({}) == []
    assert any("url" in p for p in HueMuxPlugin.check_config({"url": "not a url"}))
    assert any("verify_ssl" in p for p in HueMuxPlugin.check_config({"verify_ssl": "yes"}))
    assert any("unknown setting" in p for p in HueMuxPlugin.check_config({"bogus": 1}))


def test_check_action_and_state():
    assert HueMuxPlugin.check_action("toggle", "desk") is None
    assert HueMuxPlugin.check_action("toggle", "room:kitchen") is None
    assert HueMuxPlugin.check_action("toggle", "") is not None
    assert HueMuxPlugin.check_action("toggle", "room:") is not None
    assert HueMuxPlugin.check_action("color", {"light": "desk", "rgb": "#ff8800"}) is None
    assert HueMuxPlugin.check_action("color", {"light": "desk", "rgb": "nope"}) is not None
    assert HueMuxPlugin.check_state("on", {}, "desk") is None
    assert HueMuxPlugin.check_state("on", {}, "room:kitchen") is None
    assert HueMuxPlugin.check_state("on", {}, "") is not None
    assert HueMuxPlugin.check_value("brightness", "desk") is None


def test_connect_sends_origin_header(hx):
    hx.connect()
    assert hx.fake.headers, "no handshake recorded"
    assert hx.fake.headers[-1].get("origin") == "http://localhost"


def test_connect_publishes_states(hx):
    hx.connect()
    wait_for(lambda: hx.states.get("on[desk]") is True)
    assert hx.states.get("connected") is True
    assert hx.states.get("on[hall]") is False
    assert hx.states.get("on[room:kitchen]") is True
    assert hx.states.get("brightness[desk]") == 80
    assert hx.states.get("brightness[room:kitchen]") == 55


def test_toggle_uses_inverse_state(hx):
    hx.connect()
    hx.plugin.invoke("toggle", "desk")       # desk is on -> turn off
    hx.expect("light_toggle", rid="desk", on=False)
    hx.plugin.invoke("toggle", "hall")       # hall is off -> turn on
    hx.expect("light_toggle", rid="hall", on=True)


def test_turn_on_off(hx):
    hx.connect()
    hx.plugin.invoke("turn_on", "desk")
    hx.expect("light_toggle", rid="desk", on=True)
    hx.plugin.invoke("turn_off", "room:kitchen")
    hx.expect("room_toggle", rid="grp-kitchen", on=False)


def test_scene_favorite_color(hx):
    hx.connect()
    hx.plugin.invoke("scene", "movie")
    hx.expect("scene_recall", rid="movie")
    hx.plugin.invoke("favorite", "room:kitchen")
    hx.expect("light_favorite", rid="room:kitchen")
    hx.plugin.invoke("color", {"light": "desk", "rgb": "#ff8800"})
    hx.expect("light_color", rid="desk", r=255, g=136, b=0)


def test_brightness_adjust_and_set(hx):
    hx.connect()
    hx.plugin.set("brightness", "desk", 40)
    hx.expect("light_brightness", rid="desk", brightness=40)
    hx.plugin.adjust("brightness", "desk", 10)       # from published 80 -> 90
    hx.expect("light_brightness", rid="desk", brightness=90)
    hx.plugin.set("brightness", "room:kitchen", 10)
    hx.expect("room_brightness", rid="grp-kitchen", brightness=10)


def test_color_temp_value(hx):
    hx.connect()
    hx.plugin.set("color_temp", "desk", 100)
    hx.expect("light_color_temp", rid="desk", mirek=153)


def test_snapshot_and_event(hx):
    hx.connect()
    hx.fake.send({"type": "light_event",
                  "event": {"type": "light", "id": "desk", "on": False, "brightness": 10.0}})
    wait_for(lambda: hx.states.get("on[desk]") is False)
    assert hx.states.get("brightness[desk]") == 10
    hx.fake.send({"type": "light_event",
                  "event": {"type": "grouped_light", "id": "grp-kitchen", "brightness": 99.0}})
    wait_for(lambda: hx.states.get("brightness[room:kitchen]") == 99)


def test_drop_clears_states(hx):
    hx.connect()
    assert hx.states.get("connected") is True
    hx.fake.drop_client()
    wait_for(lambda: hx.states.get("connected") is False)
    assert hx.states.get("on[desk]") is None
