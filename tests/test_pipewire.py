"""PipewireAudio against a pretend pipewire-pulse server (tests/fakepulse.py)."""

import asyncio
import os

import pytest
from fakepulse import (
    PI_HDMI0,
    PI_HDMI0_CARD,
    PI_HDMI1,
    PI_JACK,
    SPEAKER,
    SPEAKER_ADDRESS,
    FakePulse,
    card,
    pi_hdmi,
    pi_hdmi_card,
    pi_jack,
    sink,
    speaker,
    until,
    usb_dac,
)

from couchd import pipewire
from couchd.backends import BackendError, Output
from couchd.pipewire import Pactl, PactlError, PipewireAudio, lost_sinks, parse_sinks


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(pipewire, "SETTLE", 0)
    monkeypatch.setattr(pipewire, "RETRY_MIN", 0.01)
    monkeypatch.setattr(pipewire, "RETRY_MAX", 0.01)
    monkeypatch.setattr(pipewire, "HEAL_AFTER", 0.01)
    monkeypatch.setattr(pipewire, "HEAL_EVERY", 0.01)


@pytest.fixture
async def pi():
    server = FakePulse([pi_hdmi(volume=60), pi_jack(volume=25)], default=PI_HDMI0)
    audio = PipewireAudio(server)
    await audio.start()
    await asyncio.wait_for(audio.connected.wait(), 1)
    yield server, audio
    await audio.stop()


# --- which sinks become which outputs ------------------------------------------------------


def test_pi_sinks_become_tv_jack_usb_and_bluetooth():
    raw = [speaker(), usb_dac(), pi_jack(), pi_hdmi()]
    assert [s.output for s in parse_sinks(raw)] == [
        Output("tv", "TV speakers", "tv"),
        Output("jack", "Headphone jack", "jack"),
        Output("usb:FiiO_K3-00", "FiiO K3", "usb"),
        Output(f"bluetooth:{SPEAKER_ADDRESS}", "Kitchen Speaker", "bluetooth"),
    ]


def test_bluetooth_address_falls_back_to_the_sink_name():
    s = speaker()
    del s["properties"]["api.bluez5.address"]
    assert parse_sinks([s])[0].output.id == f"bluetooth:{SPEAKER_ADDRESS}"


def test_second_hdmi_gets_its_own_stable_id():
    raw = [pi_hdmi(52, PI_HDMI1), pi_hdmi(51, PI_HDMI0)]
    outputs = [s.output for s in parse_sinks(raw)]
    assert [(o.id, o.name) for o in outputs] == [("tv", "TV speakers"), ("tv:2", "TV 2")]


def test_hdmi_recognised_by_name_when_port_type_is_missing():
    s = sink(1, "alsa_output.pci-0000_05_00.1.hdmi-stereo-extra1", port_type="Unknown")
    assert parse_sinks([s])[0].output.kind == "tv"


def test_unplugged_port_is_unavailable_and_unknown_counts_as_there():
    raw = [pi_hdmi(availability="not available"), pi_jack(availability="availability unknown")]
    assert [s.output.available for s in parse_sinks(raw)] == [False, True]


def test_virtual_sinks_are_left_out():
    null_sink = sink(9, "couch_null", api=None)
    dummy = sink(10, "auto_null", api="null")
    assert parse_sinks([null_sink, dummy, pi_jack()])[0].output.id == "jack"
    assert len(parse_sinks([null_sink, dummy, pi_jack()])) == 1


def test_volume_is_the_pulse_percentage():
    s = sink(1, PI_JACK)
    for channel in s["volume"].values():
        channel["value"] = 33423  # pactl shows 51%
    assert parse_sinks([s])[0].volume == 51


def test_volume_over_100_is_capped():
    assert parse_sinks([sink(1, PI_JACK, volume=150)])[0].volume == 100


# --- talking to the server -----------------------------------------------------------------


async def test_reports_outputs_current_volume_and_mute(pi):
    _, audio = pi
    assert [o.id for o in audio.outputs()] == ["tv", "jack"]
    assert (audio.current(), audio.volume(), audio.muted()) == ("tv", 60, False)


async def test_set_output_sets_default_and_moves_playing_streams(pi):
    server, audio = pi
    server.play(100, on=PI_HDMI0)  # YouTube
    server.play(101, on=PI_JACK)  # already there; left alone
    await audio.set_output("jack")
    assert audio.current() == "jack"
    assert server.default == PI_JACK
    assert server.stream_sinks() == {100: PI_JACK, 101: PI_JACK}
    assert ("move-sink-input", "101", PI_JACK) not in server.calls


async def test_set_output_to_an_unknown_or_unavailable_output_fails(pi):
    server, audio = pi
    with pytest.raises(BackendError):
        await audio.set_output("moon")
    server.sinks[PI_JACK]["ports"][0]["availability"] = "not available"
    await audio.refresh()
    with pytest.raises(BackendError, match="isn't available"):
        await audio.set_output("jack")


async def test_volume_and_mute_go_to_the_current_sink(pi):
    server, audio = pi
    await audio.set_volume(35)
    await audio.set_muted(True)
    assert ("set-sink-volume", PI_HDMI0, "35%") in server.calls
    assert (audio.volume(), audio.muted()) == (35, True)
    await audio.set_volume(140)
    assert ("set-sink-volume", PI_HDMI0, "100%") in server.calls


async def test_a_speaker_connecting_shows_up_as_an_output(pi):
    server, audio = pi
    changes = []
    audio.on_change(lambda: changes.append([o.id for o in audio.outputs()]))
    server.add_sink(speaker())
    bt = f"bluetooth:{SPEAKER_ADDRESS}"
    await until(lambda: bt in [o.id for o in audio.outputs()])
    await audio.set_output(bt)
    assert server.default == SPEAKER

    server.remove_sink(SPEAKER)
    await until(lambda: bt not in [o.id for o in audio.outputs()])
    assert audio.current() == "jack"  # WirePlumber's pick; AudioService moves it to the TV
    assert changes


async def test_waits_for_pipewire_then_connects():
    server = FakePulse([pi_hdmi()])
    server.up = False
    audio = PipewireAudio(server)
    await audio.start()
    try:
        await asyncio.sleep(0.03)
        assert audio.outputs() == []
        assert audio.current() is None
        server.come_up()
        await asyncio.wait_for(audio.connected.wait(), 1)
        assert [o.id for o in audio.outputs()] == ["tv"]
    finally:
        await audio.stop()


async def test_reconnects_when_pipewire_restarts(pi):
    server, audio = pi
    server.go_down()
    await until(lambda: audio.outputs() == [])
    with pytest.raises(BackendError, match="no sound output"):
        await audio.set_volume(10)
    server.come_up()
    await until(lambda: [o.id for o in audio.outputs()] == ["tv", "jack"])


async def test_stop_is_safe_before_start():
    await PipewireAudio(FakePulse([])).stop()


# --- the pactl runner ------------------------------------------------------------------------


def test_pactl_finds_the_session_socket_from_a_system_service(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    env = Pactl().env
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert env["LC_ALL"] == "C"


def test_pactl_keeps_an_existing_runtime_dir(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/somewhere")
    assert Pactl().env["XDG_RUNTIME_DIR"] == "/somewhere"


async def test_pactl_missing_is_a_pactl_error(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    pactl = Pactl()
    with pytest.raises(PactlError, match="can't run pactl"):
        await pactl.run("info")
    with pytest.raises(PactlError, match="can't run pactl"):
        async with pactl.subscribe():
            pass


async def test_pactl_failure_carries_its_message(monkeypatch, tmp_path):
    fake = tmp_path / "pactl"
    fake.write_text("#!/bin/sh\necho 'Connection failure: Connection refused' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PactlError, match="Connection refused"):
        await Pactl().json("info")


# --- a sink WirePlumber failed to make again ---------------------------------------------

HDMI_ON_CARD = {"device.name": PI_HDMI0_CARD}


def test_a_card_whose_profile_has_no_sink_has_lost_it():
    hdmi = pi_hdmi(extra=HDMI_ON_CARD)
    assert lost_sinks([pi_hdmi_card()], [pi_jack()]) == {PI_HDMI0_CARD: "output:hdmi-stereo"}
    assert lost_sinks([pi_hdmi_card()], [pi_jack(), hdmi]) == {}  # it has it
    assert lost_sinks([pi_hdmi_card(available=False)], [pi_jack()]) == {}  # TV off: no port
    assert lost_sinks([card(49, PI_HDMI0_CARD, "off")], [pi_jack()]) == {}
    assert lost_sinks([card(70, "bluez_card.AA_BB", "a2dp-sink")], []) == {}  # ALSA only


async def test_a_lost_hdmi_sink_is_made_again_and_the_tv_comes_back():
    # After the TV's standby while Kodi played: WirePlumber couldn't make the HDMI sink again
    # ("Device or resource busy") and left the card with its profile and no sink.
    server = FakePulse([pi_jack()], default=PI_JACK)
    server.cards = {PI_HDMI0_CARD: pi_hdmi_card()}
    server.card_sinks = {PI_HDMI0_CARD: pi_hdmi(extra=HDMI_ON_CARD)}
    audio = PipewireAudio(server)
    await audio.start()
    try:
        await until(lambda: "tv" in [o.id for o in audio.outputs()])
        profiles = [c for c in server.calls if c[0] == "set-card-profile"]
        assert profiles == [
            ("set-card-profile", PI_HDMI0_CARD, "off"),
            ("set-card-profile", PI_HDMI0_CARD, "output:hdmi-stereo"),
        ]
    finally:
        await audio.stop()


async def test_a_card_that_never_makes_its_sink_is_left_after_a_few_tries():
    server = FakePulse([pi_jack()], default=PI_JACK)
    server.cards = {PI_HDMI0_CARD: pi_hdmi_card()}  # and setting it up makes no sink
    audio = PipewireAudio(server)
    await audio.start()
    try:
        tries = 2 * pipewire.HEAL_TRIES
        await until(lambda: sum(c[0] == "set-card-profile" for c in server.calls) == tries)
        await asyncio.sleep(0.1)
        assert sum(c[0] == "set-card-profile" for c in server.calls) == tries
    finally:
        await audio.stop()


async def test_a_card_with_its_sink_is_left_alone():
    server = FakePulse([pi_hdmi(extra=HDMI_ON_CARD), pi_jack()], default=PI_HDMI0)
    server.cards = {PI_HDMI0_CARD: pi_hdmi_card()}
    audio = PipewireAudio(server)
    await audio.start()
    try:
        await asyncio.wait_for(audio.connected.wait(), 1)
        server.add_sink(usb_dac())  # something happens: it looks again
        await until(lambda: len(audio.outputs()) == 3)
        await asyncio.sleep(0.1)
        assert not [c for c in server.calls if c[0] == "set-card-profile"]
    finally:
        await audio.stop()
