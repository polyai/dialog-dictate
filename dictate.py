# Push-to-talk dictation using PolyAI Dialog-RSN-1 for transcription.
# Hold Right Command, speak, release: the transcript is pasted into the focused app.
# Or double-tap Right Command to talk hands-free, and tap it again to finish.
#
#   dictate setup          # store your API key in the Keychain and detect its region
#   dictate                # hotkey mode
#   dictate doctor         # check key, region and macOS permissions
#   dictate --file x.wav   # transcribe an audio file
#   dictate --debug        # log websocket events and timings to stderr
#
# Python:  from dictate import transcribe_file; print(transcribe_file("x.wav"))
#
# POLY_API_KEY and DIALOG_REALTIME_URL env vars override the stored key and region.
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import websockets

try:
    __version__ = version("dialog-dictate")
except PackageNotFoundError:
    __version__ = "dev"

# Keys only work against the region that issued them; anywhere else returns 401.
# Self-serve keys from studio.poly.ai use "studio"; US enterprise keys (studio.us.poly.ai) use "us".
REGIONS = {
    "studio": "wss://api.studio.poly.ai/v1/realtime",
    "us": "wss://api.us.poly.ai/v1/realtime",
}
KEYCHAIN_SERVICE = "dialog-dictate"
CONFIG_PATH = Path.home() / ".config" / "dialog-dictate" / "config.json"
RATE = 16000  # Dialog-RSN-1's native input rate, avoids server-side resampling
MIN_SECONDS = 0.25  # shorter taps are treated as accidental
DOUBLE_TAP_SECONDS = 0.4  # two taps this close together start hands-free mode
TIMEOUT = 15  # seconds to wait for any single server reply
DEBUG = bool(os.environ.get("DICTATE_DEBUG"))
_ALLOW_LOCAL_WS = False  # tests only: lets the fake server run on ws://127.0.0.1
T0 = time.monotonic()
CANCEL = object()  # queue sentinel: drop the utterance without transcribing


def log(*args):
    if DEBUG:
        print(f"[{time.monotonic() - T0:7.2f}]", *args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- key and config


def keychain_get() -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", getpass.getuser(), "-w"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def keychain_set(key: str):
    # `security -i` reads the command from stdin, so the key never shows up in `ps`
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    cmd = f"add-generic-password -U -s {q(KEYCHAIN_SERVICE)} -a {q(getpass.getuser())} -w {q(key)}\n"
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0 or "error" in r.stderr.lower():
        sys.exit("Couldn't save the key to the Keychain.")


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")


def require_wss(url: str, source: str) -> str:
    # The key travels in a header, so never send it over an unencrypted ws:// link
    local = _ALLOW_LOCAL_WS and url.startswith("ws://127.0.0.1:")
    if not (url.startswith("wss://") or local):
        raise SystemExit(f"{source} must start with wss:// (encrypted). Refusing to send your key to {url!r}.")
    return url


def resolve(api_key: str | None = None, url: str | None = None) -> tuple[str, str]:
    """Return (url, key): arguments first, then env vars, then Keychain and saved region."""
    key = (api_key or os.environ.get("POLY_API_KEY") or keychain_get() or "").strip()
    if not key:
        raise SystemExit("No API key found. Run `dictate setup` (or set POLY_API_KEY).")
    if url := url or os.environ.get("DIALOG_REALTIME_URL"):
        return require_wss(url, "The endpoint"), key
    cfg = load_config()
    if cfg.get("region") in REGIONS:
        return REGIONS[cfg["region"]], key
    if isinstance(cfg.get("url"), str):  # written by v0.1
        return require_wss(cfg["url"], f"The url in {CONFIG_PATH}"), key
    return REGIONS["studio"], key


def region_of(url: str) -> str:
    return next((name for name, u in REGIONS.items() if u == url), url)


# ---------------------------------------------------------------- realtime API


def connect(url: str, key: str):
    return websockets.connect(require_wss(url, "The endpoint"), additional_headers={"X-API-KEY": key}, open_timeout=10)


async def recv(ws, timeout: float = TIMEOUT) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def key_works(url: str, key: str) -> bool:
    try:
        async with connect(url, key) as ws:
            return (await recv(ws, 10))["type"] == "session.created"
    except websockets.InvalidStatus as e:
        if e.response.status_code in (401, 403):
            return False
        raise
    except (OSError, asyncio.TimeoutError):
        return False


async def find_region(key: str) -> str | None:
    """Try every region at once and return the URL of the first that accepts the key."""
    urls = list(REGIONS.values())
    results = await asyncio.gather(*(key_works(u, key) for u in urls), return_exceptions=True)
    return next((u for u, ok in zip(urls, results, strict=True) if ok is True), None)


SESSION = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        # Transcripts only come with a generated reply, so ask for a minimal text one
        "instructions": "Reply with a single period and nothing else.",
        "output_modalities": ["text"],
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE}, "turn_detection": None}},
    },
}


async def transcribe(chunks: asyncio.Queue, url: str, key: str) -> str:
    """Stream PCM16 chunks from the queue (None = end, CANCEL = drop) and return the transcript."""
    async with connect(url, key) as ws:
        await recv(ws)  # session.created
        await ws.send(json.dumps(SESSION))
        event = await recv(ws)
        if event["type"] != "session.updated":
            raise RuntimeError(event.get("error", {}).get("message", event))
        log("ws ready")

        while (chunk := await chunks.get()) is not None:
            if chunk is CANCEL:
                log("cancelled")
                return ""
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode()}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await ws.send(json.dumps({"type": "response.create"}))

        done = False
        while True:
            try:
                # After response.done the transcript can still trail by a moment
                event = await recv(ws, 3 if done else TIMEOUT)
            except asyncio.TimeoutError:
                if done:
                    return ""
                raise RuntimeError(f"no transcript after {TIMEOUT}s") from None
            t = event["type"]
            log("ws <-", t)
            if t == "conversation.item.input_audio_transcription.completed":
                await ws.send(json.dumps({"type": "response.cancel"}))  # skip the rest of the reply
                return event.get("transcript", "").strip()
            if t in ("error", "conversation.item.input_audio_transcription.failed"):
                raise RuntimeError(event.get("error", {}).get("message", t))
            if t == "response.done":
                done = True


def read_audio(path: str) -> bytes:
    """Return 16 kHz mono PCM16 from a WAV, converting other formats with macOS afconvert."""
    try:
        with wave.open(path) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2):
                return w.readframes(w.getnframes())
    except wave.Error:
        pass
    if sys.platform != "darwin":
        raise SystemExit("Need a 16 kHz mono 16-bit WAV (other formats convert only on macOS).")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "a.wav")
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", path, out], check=True)
        with wave.open(out) as w:
            return w.readframes(w.getnframes())


async def transcribe_pcm(pcm: bytes, api_key: str | None = None, url: str | None = None) -> str:
    url, key = resolve(api_key, url)
    q: asyncio.Queue = asyncio.Queue()
    for i in range(0, len(pcm), RATE // 5):
        q.put_nowait(pcm[i : i + RATE // 5])
    q.put_nowait(None)
    return await transcribe(q, url, key)


def transcribe_file(path: str, api_key: str | None = None, url: str | None = None) -> str:
    """Transcribe an audio file and return the text."""
    return asyncio.run(transcribe_pcm(read_audio(path), api_key, url))


# ---------------------------------------------------------------- commands


def setup(region: str | None):
    print("You need a PolyAI API key from the API Keys tab of https://studio.poly.ai or https://studio.us.poly.ai.")
    key = getpass.getpass("PolyAI API key (input hidden): ").strip()
    if not key:
        sys.exit("No key entered.")
    if region:
        url = REGIONS[region]
    else:
        print("Checking which region issued this key...")
        url = asyncio.run(find_region(key))
        if not url:
            sys.exit("Every region rejected the key. Check it's a valid, active PolyAI API key.")
    keychain_set(key)
    save_config({"region": region_of(url)})
    print(f"Saved key to Keychain (service '{KEYCHAIN_SERVICE}'), region: {region_of(url)}")
    print("Run `dictate` to start.")


def doctor() -> int:
    ok = True

    def check(passed: bool, label: str, fix: str = ""):
        nonlocal ok
        ok &= passed
        print(f"{'ok  ' if passed else 'FAIL'}  {label}" + ("" if passed or not fix else f"\n      fix: {fix}"))

    print(f"dictate {__version__}, Python {sys.version.split()[0]}")
    key = os.environ.get("POLY_API_KEY") or keychain_get()
    check(
        bool(key),
        "API key " + ("(POLY_API_KEY)" if os.environ.get("POLY_API_KEY") else "(Keychain)"),
        "run `dictate setup`",
    )
    url = os.environ.get("DIALOG_REALTIME_URL") or REGIONS.get(load_config().get("region", ""), REGIONS["studio"])
    print(f"      region: {region_of(url)}")
    if key:
        check(asyncio.run(key_works(url, key)), "server accepts the key", "run `dictate setup` with a current key")
    if sys.platform == "darwin":
        import HIServices
        import Quartz

        fix = "System Settings > Privacy & Security, turn it on for your terminal, then quit it with Cmd+Q"
        check(Quartz.CGPreflightListenEventAccess(), "Input Monitoring", fix)
        check(HIServices.AXIsProcessTrusted(), "Accessibility", fix)
        try:
            import sounddevice as sd

            check(True, f"Microphone: {sd.query_devices(kind='input')['name']}")
        except Exception as e:
            check(False, f"Microphone ({e})", "check Sound settings > Input")
    return 0 if ok else 1


def paste(text: str):
    log("pasting", len(text), "chars")
    from pynput import keyboard

    # Put text on the clipboard and send Cmd+V to the frontmost app
    subprocess.run(["pbcopy"], input=text.encode(), check=True)
    kb = keyboard.Controller()
    with kb.pressed(keyboard.Key.cmd):
        kb.tap("v")


# ---------------------------------------------------------------- on-screen indicator


def run_indicator():
    """Show a small pill at the bottom of the screen: `rec`, `busy` or `hide` lines on stdin.

    Runs as its own process (`dictate --indicator`) because AppKit windows need the main
    thread, which hotkey mode's asyncio loop already owns. The panel never takes focus,
    so Cmd+V still lands in the app you're dictating into.
    """
    import threading

    import AppKit
    import Quartz
    from PyObjCTools import AppHelper

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon

    h, pad = 40, 20
    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (200, h)),
        AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(AppKit.NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(AppKit.NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setIgnoresMouseEvents_(True)
    panel.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorStationary
    )
    view = panel.contentView()
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(h / 2)
    view.layer().setBackgroundColor_(Quartz.CGColorCreateGenericGray(0.08, 0.88))
    label = AppKit.NSTextField.labelWithString_("")
    view.addSubview_(label)

    font = AppKit.NSFont.systemFontOfSize_weight_(14, AppKit.NSFontWeightMedium)
    red = AppKit.NSColor.systemRedColor()
    states = {
        "rec": (red, "Listening"),
        "free": (red, "Hands-free · tap ⌘ to stop"),
        "busy": (AppKit.NSColor.systemGrayColor(), "Transcribing…"),
    }
    current = {"state": "hide", "dim": False}

    def render():
        color, text = states[current["state"]]
        if current["dim"]:
            color = color.colorWithAlphaComponent_(0.3)
        s = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
            "● ", {AppKit.NSForegroundColorAttributeName: color, AppKit.NSFontAttributeName: font}
        )
        s.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(
                text,
                {AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(), AppKit.NSFontAttributeName: font},
            )
        )
        label.setAttributedStringValue_(s)
        # Size the label to its text, fit the pill around it and centre the text both ways
        label.sizeToFit()
        lw, lh = label.frame().size.width, label.frame().size.height
        pw = round(lw + 2 * pad)
        if panel.frame().size.width != pw:
            panel.setContentSize_((pw, h))
        label.setFrameOrigin_(((pw - lw) / 2, (h - lh) / 2))

    def show(state: str):
        if state not in states:
            current["state"] = "hide"
            panel.orderOut_(None)
            return
        current["state"], current["dim"] = state, False
        render()
        frame = AppKit.NSScreen.mainScreen().visibleFrame()  # the screen you're working on
        x = frame.origin.x + (frame.size.width - panel.frame().size.width) / 2
        panel.setFrameOrigin_((x, frame.origin.y + 80))
        panel.orderFrontRegardless()

    def pulse(_timer):
        if current["state"] == "rec":
            current["dim"] = not current["dim"]
            render()

    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, pulse)

    def read_commands():
        for line in sys.stdin:
            AppHelper.callAfter(show, line.strip())
        AppHelper.callAfter(AppHelper.stopEventLoop)  # dictate exited

    threading.Thread(target=read_commands, daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)


class Indicator:
    """Drives the `dictate --indicator` process. Never lets a broken indicator stop dictation."""

    def __init__(self):
        try:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--indicator"], stdin=subprocess.PIPE, text=True
            )
        except OSError as e:
            log("indicator unavailable", e)
            self.proc = None

    def set(self, state: str):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(state + "\n")
            self.proc.stdin.flush()
        except OSError:  # helper died: carry on without it
            self.proc = None


class Gesture:
    """Turns Right Command presses into actions.

    Hold to talk, release to transcribe. Double-tap for hands-free, then tap again to
    finish; Esc cancels hands-free. Returns "start", "handsfree", "stop", "cancel" or None.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.down_at: float | None = None
        self.last_tap = float("-inf")
        self.handsfree = False
        self.ignore_release = False

    def press(self) -> str | None:
        now = self.clock()
        if self.handsfree:  # the tap that finishes hands-free
            self.handsfree, self.ignore_release = False, True
            return "stop"
        if self.down_at is not None:
            return None
        self.down_at = now
        if now - self.last_tap < DOUBLE_TAP_SECONDS:
            self.handsfree, self.ignore_release, self.last_tap = True, True, float("-inf")
            return "handsfree"
        return "start"

    def release(self) -> str | None:
        now, down, self.down_at = self.clock(), self.down_at, None
        if self.ignore_release:
            self.ignore_release = False
            return None
        if down is not None and now - down < MIN_SECONDS:
            self.last_tap = now  # maybe the first half of a double tap
        return "stop"

    def other_key(self, escape: bool = False) -> str | None:
        self.last_tap = float("-inf")
        if self.handsfree:  # typing while hands-free carries on; Esc cancels
            if not escape:
                return None
            self.handsfree = False
        return "cancel"


class Dictator:
    def __init__(self, loop: asyncio.AbstractEventLoop, url: str, key: str, indicator: Indicator | None = None):
        self.loop, self.url, self.key = loop, url, key
        self.indicator = indicator
        self.queue: asyncio.Queue | None = None
        self.stream = None
        self.started = 0.0

    def _show(self, state: str):
        if self.indicator:
            self.indicator.set(state)

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            log("audio status", status)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, bytes(indata))

    def start(self, handsfree: bool = False):
        if self.stream:
            return
        import sounddevice as sd

        self.queue = asyncio.Queue()
        self.started = time.monotonic()
        # Connect while recording so the transcript is ready soon after release
        self.loop.create_task(self._run(self.queue))
        self.stream = sd.RawInputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=RATE // 10, callback=self._on_audio
        )
        self.stream.start()
        self._show("free" if handsfree else "rec")
        print("● recording", flush=True)

    def stop(self, cancel: bool = False):
        if not self.stream:
            return
        self.stream.stop()
        self.stream.close()
        self.stream = None
        short = time.monotonic() - self.started < MIN_SECONDS
        self.queue.put_nowait(CANCEL if cancel or short else None)
        self._show("hide" if cancel or short else "busy")

    async def _run(self, queue):
        try:
            text = await transcribe(queue, self.url, self.key)
        except websockets.InvalidStatus as e:
            hint = " (key rejected: run `dictate setup` again)" if e.response.status_code in (401, 403) else ""
            print(f"[error] {e}{hint}", flush=True)
            return
        except Exception as e:
            print(f"[error] {e}", flush=True)
            return
        finally:
            if not self.stream:  # don't hide the pill if a new recording has already started
                self._show("hide")
        if text:
            print(f"→ {text!r} ({time.monotonic() - self.started:.1f}s)", flush=True)
            paste(text + " ")


async def hotkey_mode(url: str, key: str):
    # Without these, macOS silently drops the hotkey events and the Cmd+V paste
    import HIServices
    import Quartz
    from pynput import keyboard

    if not Quartz.CGPreflightListenEventAccess():
        print("[warn] terminal lacks Input Monitoring permission: hotkey won't be seen (see `dictate doctor`)")
    if not HIServices.AXIsProcessTrusted():
        print("[warn] terminal lacks Accessibility permission: transcripts won't paste (see `dictate doctor`)")

    loop = asyncio.get_running_loop()
    d = Dictator(loop, url, key, Indicator())

    # Keep the key callbacks instant: they run inside macOS's event tap, which the
    # OS disables if a callback blocks (opening the mic does). Do the work on the loop.
    g = Gesture()
    actions = {
        "start": lambda: d.start(),
        "handsfree": lambda: d.start(handsfree=True),
        "stop": lambda: d.stop(),
        "cancel": lambda: d.stop(True),  # another key while holding (Cmd+Tab, etc.): not dictation
    }

    def act(action: str | None):
        if action:
            loop.call_soon_threadsafe(actions[action])

    def on_press(k):
        if k == keyboard.Key.cmd_r:
            act(g.press())
        else:
            act(g.other_key(escape=k == keyboard.Key.esc))

    def on_release(k):
        if k == keyboard.Key.cmd_r:
            act(g.release())

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    print("Hold Right Command to dictate, or double-tap it for hands-free (tap again to stop). Ctrl+C to quit.")
    await asyncio.Event().wait()


def main():
    global DEBUG
    p = argparse.ArgumentParser(prog="dictate", description="Push-to-talk dictation with Dialog-RSN-1")
    p.add_argument("command", nargs="?", choices=["run", "setup", "doctor"], default="run")
    p.add_argument("--file", help="transcribe an audio file instead of listening")
    p.add_argument("--region", choices=REGIONS, help="setup: skip region detection")
    p.add_argument("--debug", action="store_true", help="log websocket events and timings")
    p.add_argument("--version", action="version", version=f"dictate {__version__}")
    p.add_argument("--indicator", action="store_true", help=argparse.SUPPRESS)  # internal: the on-screen pill
    args = p.parse_args()
    DEBUG = DEBUG or args.debug

    if args.indicator:
        return run_indicator()
    if args.command == "setup":
        return setup(args.region)
    if args.command == "doctor":
        sys.exit(doctor())
    if not args.file and sys.platform != "darwin":
        sys.exit("Hotkey mode needs macOS. `dictate --file x.wav` works anywhere.")

    url, key = resolve()
    log("using", url)
    try:
        if args.file:
            print(asyncio.run(transcribe_pcm(read_audio(args.file), key, url)))
        else:
            asyncio.run(hotkey_mode(url, key))
    except KeyboardInterrupt:
        pass
    except websockets.InvalidStatus as e:
        if e.response.status_code not in (401, 403):
            raise
        sys.exit("API key rejected. Run `dictate setup` again (wrong region or revoked key).")


if __name__ == "__main__":
    main()
