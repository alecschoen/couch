"""Home Assistant over MQTT (#97), with a pretend paho client."""

import asyncio
import json

import pytest

from couchd.bus import EventBus
from couchd.commands import Commands
from couchd.config import MqttConfig
from couchd.mqtt import VALUE_TEMPLATE, MqttService, node_id
from couchd.services import Context
from couchd.state import LiveState, SavedState


class Reason:
    def __init__(self, text="Success", failure=False):
        self.text = text
        self.is_failure = failure

    def __str__(self):
        return self.text


class Info:
    def wait_for_publish(self, timeout=None):
        pass


class FakeClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.published = []
        self.subscribed = []
        self.auth = None
        self.will = None
        self.target = None
        self.looping = False
        self.disconnected = False

    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive):
        self.target = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topics):
        self.subscribed += [t for t, _ in topics]

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return Info()

    # what the broker does, from paho's thread
    def connect(self, ok=True, reason="Success"):
        self.on_connect(self, None, None, Reason(reason, not ok), None)

    def drop(self):
        self.on_disconnect(self, None, None, Reason("Unspecified error", True), None)

    def message(self, topic, payload):
        msg = type("Msg", (), {"topic": topic, "payload": payload.encode()})()
        self.on_message(self, None, msg)

    def last(self, topic):
        return [p for t, p, _ in self.published if t == topic][-1]


@pytest.fixture
def ctx(tmp_path):
    bus = EventBus()
    return Context(bus, LiveState(bus), SavedState(tmp_path / "state.json"), Commands())


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def mqtt(ctx):
    calls = []

    async def power_on(args):
        calls.append("power_on")
        ctx.state.update("tv", {"power": "on"})

    async def standby(args):
        calls.append("standby")
        ctx.state.update("tv", {"power": "standby"})

    ctx.commands.add("tv.power_on", power_on)
    ctx.commands.add("tv.standby", standby)
    ctx.state.update("tv", {"power": "standby"})
    clients = []

    def factory(client_id):
        clients.append(FakeClient(client_id))
        return clients[-1]

    service = MqttService(
        ctx, MqttConfig(host="ha.local"), client_factory=factory, hostname="couch"
    )
    await service.start()
    service.calls = calls
    service.fake = clients[0]
    yield service
    await service.stop()


async def test_connects_in_the_background_with_a_last_will(mqtt, ctx):
    client = mqtt.fake
    assert client.target == ("ha.local", 1883)
    assert client.looping
    assert client.will == ("couch/status", "offline", True)
    assert client.auth is None  # no username, no login
    assert ctx.state.get("mqtt") == {"broker": "ha.local:1883", "connected": False, "error": None}
    assert client.published == []  # nothing until the broker says yes


async def test_on_connect_it_announces_the_switch_and_the_tv_state(mqtt, ctx):
    client = mqtt.fake
    client.connect()
    await settle()
    assert ctx.state.get("mqtt")["connected"] is True
    assert set(client.subscribed) == {"couch/tv/set", "homeassistant/status"}
    assert ("couch/status", "online", True) in client.published
    config = json.loads(client.last("homeassistant/switch/couch/tv/config"))
    assert config["state_topic"] == "couch/tv/state"
    assert config["command_topic"] == "couch/tv/set"
    assert config["availability_topic"] == "couch/status"
    assert config["unique_id"] == "couch_couch_tv"
    assert config["device"]["name"] == "Couch"
    assert config["value_template"] == VALUE_TEMPLATE
    assert ("couch/tv/state", "standby", True) in client.published
    # every message Home Assistant needs again after a restart is retained
    assert all(retain for _, _, retain in client.published)


async def test_the_switch_follows_the_tv(mqtt, ctx):
    client = mqtt.fake
    client.connect()
    await settle()
    ctx.state.update("tv", {"power": "on"})
    await settle()
    assert client.last("couch/tv/state") == "on"
    ctx.state.update("tv", {"power": "unknown"})
    await settle()
    assert client.last("couch/tv/state") == "unknown"
    count = len(client.published)
    ctx.state.update("tv", {"active": True})  # not the power: nothing to send
    await settle()
    assert len(client.published) == count


async def test_on_and_off_from_home_assistant(mqtt, ctx):
    client = mqtt.fake
    client.connect()
    await settle()
    client.message("couch/tv/set", "ON")
    await settle()
    assert mqtt.calls == ["power_on"]
    assert client.last("couch/tv/state") == "on"
    client.message("couch/tv/set", "OFF")
    await settle()
    assert mqtt.calls == ["power_on", "standby"]
    assert client.last("couch/tv/state") == "standby"
    client.message("couch/tv/set", "reboot")  # ignored
    await settle()
    assert mqtt.calls == ["power_on", "standby"]


async def test_a_failing_command_doesnt_break_anything(ctx, tmp_path):
    from couchd.commands import CommandError

    async def broken(args):
        raise CommandError("no TV")

    ctx.commands.add("tv.power_on", broken)
    fake = FakeClient("x")
    service = MqttService(ctx, MqttConfig(host="h"), client_factory=lambda _: fake, hostname="c")
    await service.start()
    fake.connect()
    fake.message("couch/tv/set", "ON")
    await settle()
    await service.stop()


async def test_home_assistant_coming_back_gets_the_config_again(mqtt):
    client = mqtt.fake
    client.connect()
    await settle()
    before = len([t for t, _, _ in client.published if t.endswith("/config")])
    client.message("homeassistant/status", "offline")
    client.message("homeassistant/status", "online")
    await settle()
    after = len([t for t, _, _ in client.published if t.endswith("/config")])
    assert after == before + 1


async def test_a_refused_login_is_said_once_and_retried_by_paho(mqtt, ctx, caplog):
    client = mqtt.fake
    client.connect(ok=False, reason="Not authorized")
    client.connect(ok=False, reason="Not authorized")
    await settle()
    assert ctx.state.get("mqtt") == {
        "broker": "ha.local:1883",
        "connected": False,
        "error": "not authorized",
    }
    assert sum("refused" in r.message for r in caplog.records) == 1
    assert client.published == []
    client.connect()
    await settle()
    assert ctx.state.get("mqtt")["error"] is None


async def test_reconnecting_sends_it_all_again(mqtt, ctx):
    client = mqtt.fake
    client.connect()
    await settle()
    client.drop()
    await settle()
    assert ctx.state.get("mqtt")["connected"] is False
    ctx.state.update("tv", {"power": "on"})  # while away: nothing sent
    await settle()
    assert client.last("couch/tv/state") == "standby"
    client.connect()
    await settle()
    assert client.last("couch/tv/state") == "on"


async def test_stop_says_offline_and_disconnects(ctx):
    fake = FakeClient("x")
    service = MqttService(ctx, MqttConfig(host="h"), client_factory=lambda _: fake, hostname="c")
    await service.start()
    fake.connect()
    await settle()
    await service.stop()
    assert fake.published[-1] == ("couch/status", "offline", True)
    assert fake.disconnected and not fake.looping
    fake.connect()  # paho's thread, late: ignored
    await settle()


async def test_login_reads_the_password_file(ctx, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("hunter2\n")
    fake = FakeClient("x")
    config = MqttConfig(host="h", username="couch", password_file=str(secret))
    service = MqttService(ctx, config, client_factory=lambda _: fake, hostname="c")
    await service.start()
    assert fake.auth == ("couch", "hunter2")
    await service.stop()


async def test_an_unreadable_password_file_fails_the_service(ctx, tmp_path):
    config = MqttConfig(host="h", username="couch", password_file=str(tmp_path / "missing"))
    service = MqttService(ctx, config, client_factory=FakeClient, hostname="c")
    with pytest.raises(RuntimeError, match="password_file"):
        await service.start()
    await service.stop()


async def test_topics_follow_the_config(ctx):
    fake = FakeClient("x")
    config = MqttConfig(host="h", topic="home/tv-room", discovery="ha")
    service = MqttService(ctx, config, client_factory=lambda _: fake, hostname="living.room")
    await service.start()
    fake.connect()
    await settle()
    assert set(fake.subscribed) == {"home/tv-room/tv/set", "ha/status"}
    assert json.loads(fake.last("ha/switch/living_room/tv/config"))["unique_id"] == (
        "couch_living_room_tv"
    )
    await service.stop()


def test_node_id_is_safe_for_discovery():
    assert node_id("couch") == "couch"
    assert node_id("my couch!") == "my_couch_"


def test_the_template_maps_the_tv_to_on_off_and_unknown():
    jinja2 = pytest.importorskip("jinja2")
    template = jinja2.Template(VALUE_TEMPLATE)
    assert template.render(value="on") == "ON"
    assert template.render(value="standby") == "OFF"
    assert template.render(value="unknown") == "None"


def test_the_real_client_is_paho():
    pytest.importorskip("paho.mqtt")
    from couchd.mqtt import make_client

    client = make_client("couch-test")
    assert client._client_id == b"couch-test"
