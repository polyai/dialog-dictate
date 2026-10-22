"""Tests run anywhere: no mic, keyboard, Keychain or network (a fake server on localhost)."""

import asyncio
import base64
import json
import re
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

import dictate

ONE_SECOND = bytes(dictate.RATE * 2)
KEY = "test-key-123"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(dictate, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(dictate, "keychain_get", lambda: None)
    monkeypatch.setattr(dictate, "_ALLOW_LOCAL_WS", True)
    for var in ("POLY_API_KEY", "DIALOG_REALTIME_URL", "DICTATE_ALLOW_CUSTOM_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def server():
    """A tiny stand-in for the realtime API. Set .mode to change its behaviour."""

    class State:
        mode = "happy"
        audio = b""
        keys = []

    state = State()

    async def handler(ws):
        state.keys.append(ws.request.headers.get("X-API-KEY"))
        await ws.send(json.dumps({"type": "session.created"}))
        async for raw in ws:
            event = json.loads(raw)
            t = event["type"]
            if t == "session.update":
                await ws.send(json.dumps({"type": "session.updated"}))
            elif t == "input_audio_buffer.append":
                state.audio += base64.b64decode(event["audio"])
            elif t == "response.create":
                if state.mode == "error":
                    await ws.send(json.dumps({"type": "error", "error": {"message": "boom"}}))
                    continue
                if state.mode == "late":  # transcript arrives after response.done
                    await ws.send(json.dumps({"type": "response.done"}))
                await ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": " hello world ",
                        }
                    )
                )

    async with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        state.url = f"ws://127.0.0.1:{port}/v1/realtime"
        yield state


async def test_transcribe_returns_stripped_text_and_sends_key_and_audio(server):
    assert await dictate.transcribe_pcm(ONE_SECOND, KEY, server.url) == "hello world"
    assert server.keys == [KEY]
    assert server.audio == ONE_SECOND


async def test_transcript_after_response_done(server):
    server.mode = "late"
    assert await dictate.transcribe_pcm(ONE_SECOND, KEY, server.url) == "hello world"


async def test_server_error_raises(server):
    server.mode = "error"
    with pytest.raises(RuntimeError, match="boom"):
        await dictate.transcribe_pcm(ONE_SECOND, KEY, server.url)


async def test_cancel_sends_no_commit(server):
    q = asyncio.Queue()
    q.put_nowait(ONE_SECOND)
    q.put_nowait(dictate.CANCEL)
    assert await dictate.transcribe(q, server.url, KEY) == ""


def test_refuses_plain_ws_to_remote_hosts():
    with pytest.raises(SystemExit, match="wss://"):
        dictate.require_wss("ws://api.us.poly.ai/v1/realtime", "x")


@pytest.mark.parametrize("url", ["wss://poly.ai.evil.com/x", "wss://evilpoly.ai/x", "wss://example.com/v1/realtime"])
def test_refuses_hosts_outside_poly_ai(url):
    with pytest.raises(SystemExit, match="poly.ai"):
        dictate.require_wss(url, "x")


def test_custom_endpoint_needs_opt_in_and_still_wss(monkeypatch):
    assert dictate.require_wss(dictate.REGIONS["eu"], "x")
    monkeypatch.setenv("DICTATE_ALLOW_CUSTOM_ENDPOINT", "1")
    assert dictate.require_wss("wss://example.com/v1/realtime", "x")
    with pytest.raises(SystemExit, match="wss://"):
        dictate.require_wss("ws://example.com/v1/realtime", "x")


def test_clean_flattens_newlines_and_control_characters():
    assert dictate.clean("hello\nrm -rf ~\r\n") == "hello rm -rf ~"
    assert dictate.clean(" a\tb\x07c\x1b[2J ") == "a bc[2J"


def test_resolve_order(monkeypatch):
    with pytest.raises(SystemExit, match="No API key"):
        dictate.resolve()
    monkeypatch.setenv("POLY_API_KEY", KEY)
    assert dictate.resolve() == (dictate.REGIONS["studio"], KEY)
    dictate.save_config({"region": "us"})
    assert dictate.resolve() == (dictate.REGIONS["us"], KEY)
    assert dictate.resolve("other-key", dictate.REGIONS["studio"]) == (dictate.REGIONS["studio"], "other-key")


def test_config_is_private():
    dictate.save_config({"region": "us"})
    assert dictate.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
    assert dictate.load_config() == {"region": "us"}


async def test_find_region_picks_the_region_that_accepts(monkeypatch):
    async def fake_key_works(url, key):
        return url == dictate.REGIONS["us"]

    monkeypatch.setattr(dictate, "key_works", fake_key_works)
    assert await dictate.find_region(KEY) == dictate.REGIONS["us"]


def test_read_audio_passes_16k_mono_wav_through(tmp_path):
    import wave

    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dictate.RATE)
        w.writeframes(ONE_SECOND)
    assert dictate.read_audio(str(path)) == ONE_SECOND


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_gesture_hold_to_talk():
    c = FakeClock()
    g = dictate.Gesture(c)
    assert g.press() == "start"
    c.t += 2
    assert g.release() == "stop"


def test_gesture_double_tap_goes_hands_free_until_next_tap():
    c = FakeClock()
    g = dictate.Gesture(c)
    assert g.press() == "start"  # first tap: too short, so dictate cancels it
    c.t += 0.1
    assert g.release() == "stop"
    c.t += 0.2
    assert g.press() == "handsfree"
    c.t += 0.1
    assert g.release() is None  # letting go doesn't end hands-free
    c.t += 5
    assert g.other_key() is None  # typing doesn't either
    assert g.press() == "stop"
    assert g.release() is None
    c.t += 0.2
    assert g.press() == "start"  # back to hold-to-talk, not another double tap


def test_gesture_slow_taps_are_not_a_double_tap():
    c = FakeClock()
    g = dictate.Gesture(c)
    g.press()
    c.t += 0.1
    g.release()
    c.t += 1
    assert g.press() == "start"


def test_gesture_escape_cancels_hands_free():
    c = FakeClock()
    g = dictate.Gesture(c)
    g.press()
    c.t += 0.1
    g.release()
    c.t += 0.1
    assert g.press() == "handsfree"
    g.release()
    assert g.other_key(escape=True) == "cancel"
    c.t += 1
    assert g.press() == "start"


def test_last_prints_the_saved_transcript(capsys, monkeypatch):
    monkeypatch.setattr(dictate.sys, "platform", "linux")  # don't touch the real clipboard
    assert dictate.last() == 1
    dictate.write_private(dictate.last_path(), "hello world")
    assert dictate.last_path().stat().st_mode & 0o777 == 0o600
    assert dictate.last() == 0
    assert capsys.readouterr().out == "hello world\n"


def test_setup_keeps_saved_key_and_settings(monkeypatch):
    dictate.save_config({"url": "wss://old", "hotkey": "alt_r"})
    monkeypatch.setattr(dictate, "keychain_get", lambda: KEY)
    monkeypatch.setattr(dictate, "keychain_set", lambda key: pytest.fail("key unchanged, shouldn't be re-saved"))
    monkeypatch.setattr(dictate.getpass, "getpass", lambda prompt: "")

    async def fake_find_region(key):
        assert key == KEY
        return dictate.REGIONS["us"]

    monkeypatch.setattr(dictate, "find_region", fake_find_region)
    monkeypatch.setattr(dictate.sys, "platform", "linux")  # skip the hotkey question
    dictate.setup()
    assert dictate.load_config() == {"region": "us", "hotkey": "alt_r"}


def test_hotkey_defaults_to_right_command_and_allows_any_key():
    assert dictate.hotkey_of({}) == "cmd_r"
    assert dictate.hotkey_of({"hotkey": ""}) == "cmd_r"
    assert dictate.hotkey_of({"hotkey": "f13"}) == "f13"
    assert dictate.key_label("cmd_r") == "Right Command"
    assert dictate.key_label("f13") == "f13"


def test_key_id_names_special_keys_characters_and_codes():
    from types import SimpleNamespace as K

    assert dictate.key_id(K(name="cmd_r")) == "cmd_r"
    assert dictate.key_id(K(char="§", vk=10)) == "§"
    assert dictate.key_id(K(char=None, vk=63)) == "vk:63"
    assert dictate.key_id(K(char=" ", vk=49)) == "vk:49"


def test_gesture_reset_after_auto_stop_starts_afresh():
    c = FakeClock()
    g = dictate.Gesture(c)
    g.press()
    c.t += 0.1
    g.release()
    c.t += 0.1
    assert g.press() == "handsfree"
    g.reset()  # the recording stopped itself
    assert g.release() == "stop"  # harmless: nothing is recording
    c.t += 1
    assert g.press() == "start"


def test_read_audio_passes_an_absolute_path_to_afconvert(tmp_path, monkeypatch):
    import wave

    monkeypatch.chdir(tmp_path)
    (tmp_path / "-o.m4a").write_bytes(b"not a wav")
    seen = []

    def fake_run(cmd, check):
        seen.append(cmd)
        with wave.open(cmd[-1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(dictate.RATE)
            w.writeframes(ONE_SECOND)

    monkeypatch.setattr(dictate.sys, "platform", "darwin")
    monkeypatch.setattr(dictate.subprocess, "run", fake_run)
    assert dictate.read_audio("-o.m4a") == ONE_SECOND
    assert seen[0][-2] == str(tmp_path / "-o.m4a")


class FakeStream:
    def __init__(self, **kwargs):
        self.open = False

    def start(self):
        self.open = True

    def stop(self):
        self.open = False

    def close(self):
        pass


class FakeIndicator:
    def __init__(self, up=True):
        self.up = up
        self.states = []

    def alive(self):
        return self.up

    def set(self, state):
        self.states.append(state)


@pytest.fixture
async def dictator(monkeypatch):
    """A Dictator with a fake mic and no server: returns (dictator, indicator, auto-stop count)."""
    import types

    monkeypatch.setitem(dictate.sys.modules, "sounddevice", types.SimpleNamespace(RawInputStream=FakeStream))
    monkeypatch.setattr(dictate, "WATCH_SECONDS", 0.01)

    async def fake_transcribe(queue, url, key):
        while (chunk := await queue.get()) is not None:
            if chunk is dictate.CANCEL:
                return ""
        return ""

    monkeypatch.setattr(dictate, "transcribe", fake_transcribe)
    monkeypatch.setattr(dictate, "frontmost_pid", lambda: None)
    stops = []
    ind = FakeIndicator()
    d = dictate.Dictator(asyncio.get_running_loop(), "wss://x", KEY, ind, on_auto_stop=lambda: stops.append(1))
    return d, ind, stops


async def test_recording_stops_itself_at_the_time_limit(dictator):
    d, ind, stops = dictator
    d.max_seconds = dictate.MIN_SECONDS + 0.05  # long enough not to count as a tap
    d.start(handsfree=True)
    await asyncio.sleep(dictate.MIN_SECONDS + 0.2)
    assert d.stream is None
    assert "busy" in ind.states  # transcribed, not cancelled
    assert stops == [1]


async def test_no_time_limit_when_max_minutes_is_zero(dictator):
    d, ind, stops = dictator
    d.max_seconds = None
    d.start(handsfree=True)
    await asyncio.sleep(0.05)
    assert d.stream is not None
    d.stop()


@pytest.mark.parametrize(
    "cfg, env, expected",
    [
        ({}, None, 300),
        ({"max_minutes": 30}, None, 1800),
        ({"max_minutes": 0}, None, None),
        ({"max_minutes": 30}, "2", 120),
        ({"max_minutes": "lots"}, None, 300),
        ({"max_minutes": -1}, None, 300),
        ({"max_minutes": "nan"}, None, 300),
    ],
)
def test_max_seconds_of(cfg, env, expected, monkeypatch):
    if env is None:
        monkeypatch.delenv("DICTATE_MAX_MINUTES", raising=False)
    else:
        monkeypatch.setenv("DICTATE_MAX_MINUTES", env)
    assert dictate.max_seconds_of(cfg) == expected


async def test_hands_free_ends_if_the_indicator_dies(dictator):
    d, ind, stops = dictator
    d.start(handsfree=True)
    await asyncio.sleep(0.05)
    assert d.stream is not None
    ind.up = False
    await asyncio.sleep(0.05)
    assert d.stream is None
    assert stops == [1]


async def test_hands_free_refuses_to_start_without_the_indicator(dictator):
    d, ind, stops = dictator
    ind.up = False
    d.start(handsfree=True)
    assert d.stream is None
    assert stops == [1]


async def test_hold_to_talk_carries_on_without_the_indicator(dictator):
    d, ind, stops = dictator
    ind.up = False
    d.start()
    await asyncio.sleep(0.05)
    assert d.stream is not None
    d.stop()
    assert stops == []


def _pins_and_lock():
    import tomllib

    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = {canonicalize_name(p["name"]): p for p in tomllib.loads((root / "uv.lock").read_text())["package"]}
    mac = {"sys_platform": "darwin", "platform_system": "Darwin"}

    pins = {}
    for line in project["dependencies"]:
        r = Requirement(line)
        (spec,) = r.specifier
        pins[canonicalize_name(r.name)] = spec.version if spec.operator == "==" else spec

    closure, todo = {}, [canonicalize_name(project["name"])]
    while todo:
        for dep in lock[todo.pop()].get("dependencies", []):
            name = canonicalize_name(dep["name"])
            if "marker" in dep and not Marker(dep["marker"]).evaluate(mac):
                continue
            if name not in closure:
                closure[name] = lock[name]["version"]
                todo.append(name)
    return project, pins, closure


def test_pyproject_pins_match_uv_lock():
    # uv tool install ignores uv.lock, so anything in the lock and not pinned would install unpinned.
    _, pins, closure = _pins_and_lock()
    assert pins == closure


def test_release_version_is_the_same_everywhere():
    root = Path(__file__).resolve().parent.parent
    project, _, _ = _pins_and_lock()
    tag = f"v{project['version']}"
    assert f'DICTATE_VERSION="{tag}"' in (root / "scripts" / "install.sh").read_text()
    readme = (root / "README.md").read_text()
    assert set(re.findall(r"\bv\d+\.\d+\.\d+\b", readme)) == {tag}


@pytest.mark.parametrize("front_at_release, front_now, pasted", [(111, 111, True), (111, 222, False)])
async def test_pastes_only_into_the_app_that_was_in_front_at_release(
    dictator, monkeypatch, front_at_release, front_now, pasted
):
    d, ind, stops = dictator
    fronts = iter([front_at_release, front_now])
    monkeypatch.setattr(dictate, "frontmost_pid", lambda: next(fronts))

    async def says_hello(queue, url, key):
        while await queue.get() is not None:
            pass
        return "hello\nthere"

    monkeypatch.setattr(dictate, "transcribe", says_hello)
    monkeypatch.setattr(dictate, "RESTORE_SECONDS", 0)
    pastes = []
    monkeypatch.setattr(dictate, "paste", lambda text: pastes.append(text) or (lambda: None))
    d.start()
    await asyncio.sleep(dictate.MIN_SECONDS + 0.05)
    d.stop()
    await asyncio.sleep(0.05)
    assert pastes == (["hello there "] if pasted else [])
    assert dictate.last_path().read_text() == "hello there"
    assert not d.targets
