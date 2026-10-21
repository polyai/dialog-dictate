"""Tests run anywhere: no mic, keyboard, Keychain or network (a fake server on localhost)."""

import asyncio
import base64
import json

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
    for var in ("POLY_API_KEY", "DIALOG_REALTIME_URL"):
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
