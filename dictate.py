# Copyright (c) 2026 PolyAI Limited. All rights reserved.
#
# Push-to-talk dictation with PolyAI Dialog-RSN-1.
# Hold Right Command (or the hotkey picked in setup), speak, then release to paste the
# transcript into the focused app. Double-tap it for hands-free mode and tap again to finish.
#
#   dictate                # start listening for the hotkey
#   dictate setup          # save your API key, find its region and pick a hotkey
#   dictate doctor         # check key, region, macOS permissions and microphone
#   dictate last           # print the last transcript and copy it to the clipboard
#   dictate --file x.wav   # transcribe an audio file
#   dictate --debug        # log websocket events and timings to stderr
#
# Python:  from dictate import transcribe_file; print(transcribe_file("x.wav"))
#
# POLY_API_KEY and DIALOG_REALTIME_URL override the saved key and region.
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlparse

import websockets

try:
    __version__ = version("dialog-dictate")
except PackageNotFoundError:
    __version__ = "dev"

# Keys only work against the region that issued them; anywhere else returns 401.
# Self-serve keys use "studio"; enterprise keys use "us", "uk" or "eu".
REGIONS = {
    "studio": "wss://api.studio.poly.ai/v1/realtime",
    "us": "wss://api.us.poly.ai/v1/realtime",
    "uk": "wss://api.uk.poly.ai/v1/realtime",
    "eu": "wss://api.eu.poly.ai/v1/realtime",
}
KEYCHAIN_SERVICE = "dialog-dictate"
CONFIG_PATH = Path.home() / ".config" / "dialog-dictate" / "config.json"
RATE = 16000  # Dialog-RSN-1's native input rate, avoids server-side resampling
MIN_SECONDS = 0.25  # shorter taps are treated as accidental
DEFAULT_MAX_MINUTES = 5  # recordings stop after this; caps held ones too, in case a key release is missed
WATCH_SECONDS = 0.5  # how often a recording checks the time limit and the pill
DOUBLE_TAP_SECONDS = 0.4  # two taps this close together start hands-free mode
TIMEOUT = 15  # seconds to wait for any single server reply
RESTORE_SECONDS = 0.5  # time the focused app gets to read the pasted text before the old clipboard comes back
DEFAULT_HOTKEY = "cmd_r"
# Friendlier names for common hotkeys; any other key shows its config name (e.g. "f13")
KEY_LABELS = {
    "cmd_r": "Right Command",
    "alt_r": "Right Option",
    "ctrl_r": "Right Control",
    "shift_r": "Right Shift",
    "cmd": "Left Command",
    "alt": "Left Option",
    "ctrl": "Left Control",
    "shift": "Left Shift",
    "caps_lock": "Caps Lock",
}
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


def write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def save_config(data: dict):
    write_private(CONFIG_PATH, json.dumps(data, indent=2) + "\n")


def hotkey_of(cfg: dict) -> str:
    h = cfg.get("hotkey")
    return h if isinstance(h, str) and h else DEFAULT_HOTKEY


def max_seconds_of(cfg: dict) -> float | None:
    """Recording limit from DICTATE_MAX_MINUTES or the config's max_minutes; 0 means no limit."""
    m = os.environ.get("DICTATE_MAX_MINUTES", cfg.get("max_minutes", DEFAULT_MAX_MINUTES))
    try:
        m = float(m)
    except (TypeError, ValueError):
        m = -1
    if not math.isfinite(m) or m < 0:
        print(f"[warn] max_minutes must be 0 or more; using {DEFAULT_MAX_MINUTES}", flush=True)
        m = DEFAULT_MAX_MINUTES
    return m * 60 if m > 0 else None


def key_label(hotkey: str) -> str:
    return KEY_LABELS.get(hotkey, hotkey)


def key_id(k) -> str:
    """Config name for a pynput key: a special key's name ("cmd_r", "f13"), a character, or "vk:<code>"."""
    if name := getattr(k, "name", None):
        return name
    if k.char and k.char.isprintable() and not k.char.isspace():
        return k.char
    return f"vk:{k.vk}"


def capture_key() -> str | None:
    """Wait for the next key press and return its config name, or None if it was Enter."""
    import termios

    from pynput import keyboard

    pressed = []

    def on_press(k):
        pressed.append(k)
        return False  # stop listening

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
    if sys.stdin.isatty():
        termios.tcflush(sys.stdin, termios.TCIFLUSH)  # drop the keystroke the terminal also received
    return None if pressed[0] == keyboard.Key.enter else key_id(pressed[0])


def last_path() -> Path:
    return CONFIG_PATH.with_name("last.txt")


def require_wss(url: str, source: str) -> str:
    # The key travels in a header, so never send it over an unencrypted ws:// link,
    # or to a host outside poly.ai unless the user has opted in
    local = _ALLOW_LOCAL_WS and url.startswith("ws://127.0.0.1:")
    if local:
        return url
    if not url.startswith("wss://"):
        raise SystemExit(f"{source} must start with wss:// (encrypted). Refusing to send your key to {url!r}.")
    host = urlparse(url).hostname or ""
    if not (host == "poly.ai" or host.endswith(".poly.ai")) and not os.environ.get("DICTATE_ALLOW_CUSTOM_ENDPOINT"):
        raise SystemExit(
            f"{source} points at {host!r}, which isn't a poly.ai host. Refusing to send your key there.\n"
            "If you trust it, set DICTATE_ALLOW_CUSTOM_ENDPOINT=1."
        )
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
    if isinstance(cfg.get("url"), str):  # config saved by v0.1
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
    except (TimeoutError, OSError):
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
            except TimeoutError:
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
        # Absolute, so a name starting with "-" can't be read as an afconvert option
        src = os.path.abspath(path)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", src, out], check=True)
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


def setup():
    cfg = load_config()
    saved = keychain_get()
    print("You need a PolyAI API key from the API Keys tab of Agent Studio.")
    hint = ", Enter keeps the saved one" if saved else ""
    key = getpass.getpass(f"PolyAI API key (input hidden{hint}): ").strip() or saved
    if not key:
        sys.exit("No key entered.")
    print("Checking which region issued this key...")
    url = asyncio.run(find_region(key))
    if not url:
        sys.exit("Every region rejected the key. Check it's a valid, active PolyAI API key.")
    if key != saved:
        keychain_set(key)
    cfg.pop("url", None)  # v0.1 setting, replaced by region
    cfg["region"] = region_of(url)
    print(f"Key saved in Keychain (service '{KEYCHAIN_SERVICE}'), region: {region_of(url)}")

    if sys.platform == "darwin":
        import Quartz

        current = key_label(hotkey_of(cfg))
        if Quartz.CGPreflightListenEventAccess():
            print(f"\nHotkey: press the key to hold while dictating (Enter keeps {current})...")
            if hotkey := capture_key():
                cfg["hotkey"] = hotkey
            print(f"Hotkey: {key_label(hotkey_of(cfg))}")
            if len(hotkey_of(cfg)) == 1:
                print("Note: this key still types into the focused app. Modifiers or F13-F19 work best.")
        else:
            # Can't see key presses yet; they work after the first `dictate` run grants Input Monitoring
            print(f"\nHotkey: {current}. Run `dictate setup` again after granting Input Monitoring to change it.")

    save_config(cfg)
    print("\nSaved. Run `dictate` to start.")


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
        print(f"      hotkey: {key_label(hotkey_of(load_config()))}")
        try:
            import sounddevice as sd

            check(True, f"Microphone: {sd.query_devices(kind='input')['name']}")
        except Exception as e:
            check(False, f"Microphone ({e})", "check Sound settings > Input")
    return 0 if ok else 1


def last() -> int:
    try:
        text = last_path().read_text()
    except OSError:
        print("Nothing dictated yet.", file=sys.stderr)
        return 1
    print(text)
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        print("(copied to clipboard)", file=sys.stderr)
    return 0


def clean(text: str) -> str:
    """Collapse newlines and tabs and drop control characters, so a paste can never press Enter."""
    return "".join(c for c in " ".join(text.split()) if c.isprintable())


def frontmost_pid() -> int | None:
    """PID of the app owning the frontmost normal window (skips the pill, which floats above)."""
    if sys.platform != "darwin":
        return None
    import Quartz

    # Ask the window server rather than NSWorkspace, whose answer goes stale without a
    # Cocoa run loop. Windows come back front to back; layer 0 is ordinary app windows.
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    return next((w.get("kCGWindowOwnerPID") for w in windows or [] if w.get("kCGWindowLayer") == 0), None)


def paste(text: str):
    """Paste text into the frontmost app via the clipboard. Returns a function that puts the old clipboard back."""
    log("pasting", len(text), "chars")
    from AppKit import NSPasteboard, NSPasteboardContentsCurrentHostOnly, NSPasteboardItem, NSPasteboardTypeString
    from pynput import keyboard

    pb = NSPasteboard.generalPasteboard()
    saved = [{t: item.dataForType_(t) for t in item.types()} for item in pb.pasteboardItems() or []]
    # Keep dictations off Universal Clipboard, and ask clipboard managers not to keep or
    # show them (https://nspasteboard.org)
    pb.prepareForNewContentsWithOptions_(NSPasteboardContentsCurrentHostOnly)
    pb.setString_forType_(text, NSPasteboardTypeString)
    for marker in ("org.nspasteboard.TransientType", "org.nspasteboard.ConcealedType"):
        pb.setString_forType_("", marker)
    ours = pb.changeCount()
    kb = keyboard.Controller()
    with kb.pressed(keyboard.Key.cmd):
        kb.tap("v")

    def restore():
        if pb.changeCount() != ours:  # something else was copied since: keep it
            return
        pb.clearContents()
        items = []
        for types in saved:
            item = NSPasteboardItem.alloc().init()
            for t, data in types.items():
                if data is not None:
                    item.setData_forType_(data, t)
            items.append(item)
        if items:
            pb.writeObjects_(items)

    return restore


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

    h, pad, dot, gap = 36, 16, 8, 8
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

    def rgb(hex_: int, alpha: float = 1.0):
        return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            (hex_ >> 16) / 255, ((hex_ >> 8) & 0xFF) / 255, (hex_ & 0xFF) / 255, alpha
        )

    # PolyAI colours: grey-100 (#161617) pill with a faint edge, brand-50 (#D9EE50) dot while listening
    view = panel.contentView()
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(h / 2)
    view.layer().setBackgroundColor_(rgb(0x161617, 0.94).CGColor())
    view.layer().setBorderWidth_(1)
    view.layer().setBorderColor_(rgb(0xFFFFFF, 0.08).CGColor())

    # The dot is its own layer rather than a "●" glyph, so it sits dead centre whatever the font
    dot_layer = Quartz.CALayer.layer()
    dot_layer.setCornerRadius_(dot / 2)
    dot_layer.setFrame_(((pad, (h - dot) / 2), (dot, dot)))
    view.layer().addSublayer_(dot_layer)
    label = AppKit.NSTextField.labelWithString_("")
    view.addSubview_(label)

    # Brand typeface if installed, else the system font
    font = AppKit.NSFont.fontWithName_size_("Proxima Nova Semibold", 14) or AppKit.NSFont.systemFontOfSize_weight_(
        14, AppKit.NSFontWeightMedium
    )
    label.setFont_(font)
    label.setTextColor_(rgb(0xF5F5F5))

    brand = rgb(0xD9EE50)
    states = {  # state: (dot colour, text, pulse)
        "rec": (brand, "Listening", True),
        "free": (brand, "Hands-free · tap ⌘ to stop", True),
        "busy": (rgb(0x858589), "Transcribing…", False),
    }
    current = {"state": "hide"}

    def render():
        color, text, pulse = states[current["state"]]
        dot_layer.setBackgroundColor_(color.CGColor())
        dot_layer.removeAllAnimations()
        if pulse:  # breathe rather than blink
            anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            anim.setFromValue_(1.0)
            anim.setToValue_(0.3)
            anim.setDuration_(0.7)
            anim.setAutoreverses_(True)
            anim.setRepeatCount_(float("inf"))
            anim.setTimingFunction_(
                Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut)
            )
            dot_layer.addAnimation_forKey_(anim, "pulse")
        label.setStringValue_(text)
        label.sizeToFit()
        lw, lh = label.frame().size.width, label.frame().size.height
        # NSTextField pads its text a little on each side; measure the glyphs themselves
        inset = (lw - label.attributedStringValue().size().width) / 2
        pw = round(pad + dot + gap + (lw - 2 * inset) + pad)
        if panel.frame().size.width != pw:
            panel.setContentSize_((pw, h))
        # Centre the capitals on the pill, not the line box (which includes descender space)
        baseline = (h - font.capHeight()) / 2
        label.setFrameOrigin_((pad + dot + gap - inset, baseline - (lh - label.firstBaselineOffsetFromTop())))

    def fade(alpha: float, done=None):
        def animate(ctx):
            ctx.setDuration_(0.15)
            panel.animator().setAlphaValue_(alpha)

        AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(animate, done)

    def show(state: str):
        if state not in states:
            current["state"] = "hide"
            # Only order out if nothing re-showed the pill while it was fading
            fade(0.0, lambda: current["state"] == "hide" and panel.orderOut_(None))
            return
        was_hidden = current["state"] == "hide"
        current["state"] = state
        render()
        frame = AppKit.NSScreen.mainScreen().visibleFrame()  # screen with the focused window
        x = frame.origin.x + (frame.size.width - panel.frame().size.width) / 2
        panel.setFrameOrigin_((round(x), frame.origin.y + 80))
        if was_hidden:
            panel.setAlphaValue_(0.0)
        panel.orderFrontRegardless()
        fade(1.0)

    def read_commands():
        for line in sys.stdin:
            AppHelper.callAfter(show, line.strip())
        AppHelper.callAfter(AppHelper.stopEventLoop)  # stdin closed: dictate has exited

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

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def set(self, state: str):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(state + "\n")
            self.proc.stdin.flush()
        except OSError:  # helper died: carry on without it
            self.proc = None


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Gesture:
    """Turns hotkey presses into actions.

    Hold to talk, release to transcribe. Double-tap for hands-free, then tap again to
    finish; Esc cancels hands-free. Returns "start", "handsfree", "stop", "cancel" or None.
    Locked, because the key listener thread and the asyncio loop (reset) both call it.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self.down_at: float | None = None
        self.last_tap = float("-inf")
        self.handsfree = False
        self.ignore_release = False

    @_locked
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

    @_locked
    def release(self) -> str | None:
        now, down, self.down_at = self.clock(), self.down_at, None
        if self.ignore_release:
            self.ignore_release = False
            return None
        if down is not None and now - down < MIN_SECONDS:
            self.last_tap = now  # maybe the first half of a double tap
        return "stop"

    @_locked
    def other_key(self, escape: bool = False) -> str | None:
        self.last_tap = float("-inf")
        if self.handsfree:  # typing while hands-free carries on; Esc cancels
            if not escape:
                return None
            self.handsfree = False
        return "cancel"

    @_locked
    def reset(self):
        """Forget any hold or hands-free in progress."""
        self.down_at, self.handsfree, self.ignore_release = None, False, False


class Dictator:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        url: str,
        key: str,
        indicator: Indicator | None = None,
        on_auto_stop=None,
        max_seconds: float | None = DEFAULT_MAX_MINUTES * 60,
    ):
        self.loop, self.url, self.key = loop, url, key
        self.max_seconds = max_seconds
        self.indicator = indicator
        self.on_auto_stop = on_auto_stop  # called when a recording stops without the user asking
        self.queue: asyncio.Queue | None = None
        self.targets: dict[asyncio.Queue, int | None] = {}  # app in front when each recording stopped
        self.stream = None
        self.started = 0.0
        self.handsfree = False

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
        if handsfree and not (self.indicator and self.indicator.alive()):
            # Once the key is let go, the pill is the only sign the mic is on
            print(
                "[warn] hands-free needs the on-screen indicator, which isn't running. Hold the key instead.",
                flush=True,
            )
            self._auto_stop()
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
        self.handsfree = handsfree
        self._show("free" if handsfree else "rec")
        print("● recording", flush=True)
        self.loop.call_later(WATCH_SECONDS, self._watch, self.stream)

    def _watch(self, stream):
        if self.stream is not stream:  # stopped, or a newer recording has its own watch
            return
        if self.max_seconds and time.monotonic() - self.started >= self.max_seconds:
            print(f"[warn] stopped recording after {self.max_seconds / 60:g} minutes (max_minutes)", flush=True)
        elif self.handsfree and not (self.indicator and self.indicator.alive()):
            print("[warn] on-screen indicator stopped: ending hands-free", flush=True)
        else:
            self.loop.call_later(WATCH_SECONDS, self._watch, stream)
            return
        self.stop()  # still transcribe what was said
        self._auto_stop()

    def _auto_stop(self):
        if self.on_auto_stop:
            self.on_auto_stop()

    def stop(self, cancel: bool = False):
        if not self.stream:
            return
        self.stream.stop()
        self.stream.close()
        self.stream = None
        short = time.monotonic() - self.started < MIN_SECONDS
        self.targets[self.queue] = frontmost_pid()
        self.queue.put_nowait(CANCEL if cancel or short else None)
        self._show("hide" if cancel or short else "busy")

    async def _run(self, queue):
        try:
            text = clean(await transcribe(queue, self.url, self.key))
        except websockets.InvalidStatus as e:
            hint = " (key rejected: run `dictate setup` again)" if e.response.status_code in (401, 403) else ""
            print(f"[error] {e}{hint}", flush=True)
            return
        except Exception as e:
            print(f"[error] {e}", flush=True)
            return
        finally:
            target = self.targets.pop(queue, None)
            if not self.stream:  # don't hide the pill if a new recording has already started
                self._show("hide")
        if text:
            print(f"→ {text!r} ({time.monotonic() - self.started:.1f}s)", flush=True)
            write_private(last_path(), text)  # for `dictate last` if the paste lands in the wrong place
            if frontmost_pid() != target:
                # Focus moved while transcribing; don't type into whatever is in front now
                print("[info] you switched apps before the text was ready, so it wasn't pasted. Run `dictate last`.")
                return
            restore = paste(text + " ")
            await asyncio.sleep(RESTORE_SECONDS)
            restore()


async def hotkey_mode(url: str, key: str):
    # Without these, macOS silently drops the hotkey events and the Cmd+V paste
    import HIServices
    import Quartz
    from pynput import keyboard

    if not Quartz.CGPreflightListenEventAccess():
        print("[warn] terminal lacks Input Monitoring permission: hotkey won't be seen (see `dictate doctor`)")
    if not HIServices.AXIsProcessTrusted():
        print("[warn] terminal lacks Accessibility permission: transcripts won't paste (see `dictate doctor`)")

    cfg = load_config()
    hotkey = hotkey_of(cfg)
    loop = asyncio.get_running_loop()
    # Keep the key callbacks instant: they run inside macOS's event tap, which the
    # OS disables if a callback blocks (opening the mic does). Do the work on the loop.
    g = Gesture()
    d = Dictator(loop, url, key, Indicator(), on_auto_stop=g.reset, max_seconds=max_seconds_of(cfg))
    actions = {
        "start": lambda: d.start(),
        "handsfree": lambda: d.start(handsfree=True),
        "stop": lambda: d.stop(),
        "cancel": lambda: d.stop(True),  # another key pressed while holding (e.g. Cmd+Tab)
    }

    def act(action: str | None):
        if action:
            loop.call_soon_threadsafe(actions[action])

    held = False  # holding a non-modifier key auto-repeats its press events: only the first counts

    def on_press(k):
        nonlocal held
        if key_id(k) == hotkey:
            if not held:
                held = True
                act(g.press())
        else:
            act(g.other_key(escape=k == keyboard.Key.esc))

    def on_release(k):
        nonlocal held
        if key_id(k) == hotkey:
            held = False
            act(g.release())

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    print(f"Hold {key_label(hotkey)} to dictate, or double-tap it for hands-free (tap again to stop). Ctrl+C to quit.")
    await asyncio.Event().wait()


def main():
    global DEBUG
    p = argparse.ArgumentParser(
        prog="dictate",
        description="Push-to-talk dictation with Dialog-RSN-1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands:\n"
        "  setup        save your API key, detect its region and pick a hotkey\n"
        "  doctor       check your key, region, permissions and microphone\n"
        "  last         print the last transcript and copy it to the clipboard",
    )
    p.add_argument("command", nargs="?", choices=["setup", "doctor", "last"], help=argparse.SUPPRESS)
    p.add_argument("--file", help="transcribe an audio file and print the text")
    p.add_argument("--debug", action="store_true", help="log websocket events and timings to stderr")
    p.add_argument("--version", action="version", version=f"dictate {__version__}")
    p.add_argument("--indicator", action="store_true", help=argparse.SUPPRESS)  # runs the on-screen pill process
    args = p.parse_args()
    DEBUG = DEBUG or args.debug

    if args.indicator:
        return run_indicator()
    if args.command == "setup":
        return setup()
    if args.command == "doctor":
        sys.exit(doctor())
    if args.command == "last":
        sys.exit(last())
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
