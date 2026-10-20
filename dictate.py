# Push-to-talk dictation using PolyAI Dialog-RSN-1 for transcription.
# Hold Right Option, speak, release: the transcript is pasted into the focused app.
#
#   dictate setup          # store your API key in the Keychain and detect its region
#   dictate                # hotkey mode
#   dictate --file x.wav   # transcribe a 16 kHz mono WAV
#   dictate --debug        # log every key, websocket event and paste to stderr
#
# POLY_API_KEY and DIALOG_REALTIME_URL env vars override the stored key and region.
import argparse, asyncio, base64, getpass, json, os, subprocess, sys, time, wave
from pathlib import Path

import sounddevice as sd
import websockets
from pynput import keyboard

# Keys only work against the region that issued them; anywhere else returns 401
REGIONS = {
    "studio": "wss://api.studio.poly.ai/v1/realtime",
    "us": "wss://api.us.poly.ai/v1/realtime",
}
KEYCHAIN_SERVICE = "dialog-dictate"
CONFIG_PATH = Path.home() / ".config" / "dialog-dictate" / "config.json"
HOTKEY = keyboard.Key.alt_r
RATE = 16000  # Dialog-RSN-1's native input rate, avoids server-side resampling
DEBUG = bool(os.environ.get("DICTATE_DEBUG"))
T0 = time.monotonic()

# Resolved in main() from env vars, then Keychain / config file
URL = API_KEY = None


def log(*args):
    if DEBUG:
        print(f"[{time.monotonic() - T0:7.2f}]", *args, file=sys.stderr, flush=True)


def keychain_get() -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                        "-a", getpass.getuser(), "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def keychain_set(key: str):
    # -U updates an existing item. The key is briefly visible in this machine's
    # process list; `security` has no non-interactive way to read it from stdin.
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                    "-a", getpass.getuser(), "-w", key], check=True, capture_output=True)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def key_works(url: str, key: str) -> bool:
    try:
        async with websockets.connect(url, additional_headers={"X-API-KEY": key}, open_timeout=10) as ws:
            return json.loads(await ws.recv())["type"] == "session.created"
    except websockets.InvalidStatus as e:
        if e.response.status_code in (401, 403):
            return False
        raise


def require_wss(url: str, source: str) -> str:
    # The key travels in a header, so never send it over an unencrypted ws:// link
    if not url.startswith("wss://"):
        sys.exit(f"{source} must start with wss:// (encrypted). Refusing to send your key to {url!r}.")
    return url


async def find_region(key: str) -> str | None:
    """Return the URL of the first region that accepts the key."""
    override = os.environ.get("DIALOG_REALTIME_URL")
    if override:
        require_wss(override, "DIALOG_REALTIME_URL")
    for url in [override] if override else REGIONS.values():
        if await key_works(url, key):
            return url
    return None


def setup():
    key = getpass.getpass("PolyAI API key (input hidden): ").strip()
    if not key:
        sys.exit("No key entered.")
    print("Checking which region issued this key...")
    url = asyncio.run(find_region(key))
    if not url:
        sys.exit("Key was rejected by every region. Check it's a valid, active PolyAI API key.")
    keychain_set(key)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"url": url}, indent=2) + "\n")
    region = next((name for name, u in REGIONS.items() if u == url), url)
    print(f"Saved key to Keychain (service '{KEYCHAIN_SERVICE}'), region: {region}")
    print("Run `dictate` to start.")


SESSION = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        # Transcripts only come from a generated reply, so ask for a minimal text one
        "instructions": "Reply with a single period and nothing else.",
        "output_modalities": ["text"],
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE}, "turn_detection": None}},
    },
}


async def transcribe(chunks: asyncio.Queue) -> str:
    """Stream PCM16 chunks from the queue (None = end of utterance) and return the transcript."""
    async with websockets.connect(URL, additional_headers={"X-API-KEY": API_KEY}) as ws:
        await ws.recv()  # session.created
        await ws.send(json.dumps(SESSION))
        event = json.loads(await ws.recv())
        if event["type"] != "session.updated":
            raise RuntimeError(event.get("error", {}).get("message", event))

        log("ws ready")
        n = 0
        while (chunk := await chunks.get()) is not None:
            n += 1
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(chunk).decode()}))
        log(f"sent {n} audio chunks, committing")
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await ws.send(json.dumps({"type": "response.create"}))

        async for raw in ws:
            event = json.loads(raw)
            t = event["type"]
            log("ws <-", t)
            if t == "conversation.item.input_audio_transcription.completed":
                # Got what we need; skip the rest of the reply
                await ws.send(json.dumps({"type": "response.cancel"}))
                return event.get("transcript", "").strip()
            if t == "error":
                raise RuntimeError(event["error"]["message"])
            if t == "response.done":
                return ""


def paste(text: str):
    log("pasting", repr(text))
    # Put text on the clipboard and send Cmd+V to the frontmost app
    subprocess.run(["pbcopy"], input=text.encode(), check=True)
    kb = keyboard.Controller()
    with kb.pressed(keyboard.Key.cmd):
        kb.tap("v")


class Dictator:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue | None = None
        self.stream: sd.RawInputStream | None = None
        self.started = 0.0

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            log("audio status", status)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, bytes(indata))

    def start(self):
        log("start()", "already recording, ignored" if self.stream else "")
        if self.stream:
            return
        self.queue = asyncio.Queue()
        self.started = time.monotonic()
        # Connect while recording so the transcript is ready soon after release
        asyncio.run_coroutine_threadsafe(self._run(self.queue), self.loop)
        # Raw stream hands the callback plain PCM16 bytes, so numpy isn't needed
        self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                        blocksize=RATE // 10, callback=self._on_audio)
        self.stream.start()
        print("● recording", flush=True)

    def stop(self):
        log("stop()", "" if self.stream else "not recording, ignored")
        if not self.stream:
            return
        self.stream.stop()
        self.stream.close()
        self.stream = None
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)

    async def _run(self, queue):
        try:
            text = await transcribe(queue)
        except websockets.InvalidStatus as e:
            hint = " (key rejected: run `dictate setup` again)" if e.response.status_code in (401, 403) else ""
            print(f"[error] {e}{hint}", flush=True)
            return
        except Exception as e:
            print(f"[error] {e}", flush=True)
            return
        print(f"→ {text!r} ({time.monotonic() - self.started:.1f}s)", flush=True)
        if text:
            paste(text + " ")


async def hotkey_mode():
    # Without these, macOS silently drops the hotkey events and the Cmd+V paste
    import HIServices, Quartz
    if not Quartz.CGPreflightListenEventAccess():
        print("[warn] terminal lacks Input Monitoring permission: hotkey won't be seen")
    if not HIServices.AXIsProcessTrusted():
        print("[warn] terminal lacks Accessibility permission: transcripts won't paste")

    loop = asyncio.get_running_loop()
    d = Dictator(loop)
    # Keep the key callbacks instant: they run inside macOS's event tap, which the
    # OS disables if a callback blocks (opening the mic does), and pynput never
    # re-enables it, so the hotkey would go dead. Do the real work on the loop.
    def on_key(k, pressed):
        log("key", "down" if pressed else "up", k)
        if k == HOTKEY:
            loop.call_soon_threadsafe(d.start if pressed else d.stop)

    listener = keyboard.Listener(on_press=lambda k: on_key(k, True),
                                 on_release=lambda k: on_key(k, False))
    listener.start()
    print("Hold Right Option to dictate. Ctrl+C to quit.")
    await asyncio.Event().wait()


async def file_mode(path: str):
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "need 16 kHz mono PCM16 WAV"
        pcm = w.readframes(w.getnframes())
    q = asyncio.Queue()
    for i in range(0, len(pcm), RATE // 5):
        q.put_nowait(pcm[i:i + RATE // 5])
    q.put_nowait(None)
    print(await transcribe(q))


def main():
    global URL, API_KEY, DEBUG
    parser = argparse.ArgumentParser(prog="dictate", description="Push-to-talk dictation with Dialog-RSN-1")
    parser.add_argument("command", nargs="?", choices=["run", "setup"], default="run")
    parser.add_argument("--file", help="transcribe a 16 kHz mono PCM16 WAV instead of listening")
    parser.add_argument("--debug", action="store_true", help="log keys, websocket events and pastes")
    args = parser.parse_args()
    DEBUG = DEBUG or args.debug

    if args.command == "setup":
        setup()
        return

    API_KEY = os.environ.get("POLY_API_KEY") or keychain_get()
    if not API_KEY:
        sys.exit("No API key found. Run `dictate setup` first.")
    if env_url := os.environ.get("DIALOG_REALTIME_URL"):
        URL = require_wss(env_url, "DIALOG_REALTIME_URL")
    elif cfg_url := load_config().get("url"):
        URL = require_wss(cfg_url, f"The url in {CONFIG_PATH}")
    else:
        URL = REGIONS["studio"]
    log("using", URL)

    try:
        asyncio.run(file_mode(args.file) if args.file else hotkey_mode())
    except KeyboardInterrupt:
        pass
    except websockets.InvalidStatus as e:
        if e.response.status_code not in (401, 403):
            raise
        sys.exit("API key rejected. Run `dictate setup` again (wrong region or revoked key).")


if __name__ == "__main__":
    main()
