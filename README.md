# dialog-dictate

Push-to-talk dictation for macOS, powered by [PolyAI Dialog-RSN-1](https://docs.poly.ai/api-reference/dialog-rsn-1/reference).

Hold **Right Command**, speak, let go. Your words are pasted wherever your cursor is.

For longer dictation, **double-tap Right Command** to go hands-free, then tap it once more to finish (Esc cancels).

While it's listening, a **Listening** pill appears at the bottom of the screen; it switches to **Transcribing…** when you let go and disappears once the text is pasted.

## Try it in a minute

1. Create an API key in the **API Keys** tab of Agent Studio.
2. Install and paste your key when asked:

   ```sh
   curl -LsSf https://raw.githubusercontent.com/polyai/dialog-dictate/main/scripts/install.sh | bash
   ```

   Or by hand: `uv tool install git+https://github.com/polyai/dialog-dictate && dictate setup` (get uv with `brew install uv`).

3. Run `dictate`, hold Right Command and talk. Ctrl+C stops it.

The first run asks macOS to give **your terminal app** Microphone, Input Monitoring and Accessibility. Allow them in System Settings > Privacy & Security, then **quit the terminal with Cmd+Q and reopen it**. If something doesn't work, run `dictate doctor`.

## Commands

```sh
dictate                  # hold Right Command to dictate, or double-tap for hands-free
dictate setup            # save your key, find its region and pick a hotkey
dictate doctor           # check key, region, permissions and microphone
dictate last             # print the last transcript and copy it to the clipboard
dictate --file memo.wav  # transcribe a file (prints the text)
dictate --debug          # show websocket events and timings
```

From Python:

```python
from dictate import transcribe_file

print(transcribe_file("memo.wav"))  # uses POLY_API_KEY or the key saved by `dictate setup`
```

## Good to know

- **Your clipboard is kept.** Text is pasted via the clipboard, then whatever you had copied is put back. If a paste lands in the wrong place, `dictate last` gets it back (the last transcript is kept in `~/.config/dialog-dictate/last.txt`).
- **Pick another hotkey** by running `dictate setup` again and pressing the key you want. Any key works; modifiers or F13–F19 are best, since other keys still type into the focused app. Press Enter at the API key prompt to keep your saved key.
- **Audio goes to PolyAI's API** in your key's region, using your key. See [limits and pricing](https://docs.poly.ai/api-reference/dialog-rsn-1/limits).
- **Quick taps are ignored**, and pressing another key while holding Right Command (Cmd+Tab, say) cancels that recording.
- **Keys belong to one host.** `dictate setup` finds yours: `studio` (self-serve) or one of the `us`, `uk` and `eu` enterprise regions.
- `POLY_API_KEY` and `DIALOG_REALTIME_URL` (must be `wss://`) override the saved key and region.
- Update with `uv tool upgrade dialog-dictate`; remove with `uv tool uninstall dialog-dictate`.

## Development

```sh
git clone https://github.com/polyai/dialog-dictate && cd dialog-dictate
uv run pytest            # tests use a fake local server: no mic, key or network
uv run ruff check . && uv run ruff format --check .
uv tool install -e .     # `dictate` now runs your checkout
```

Everything lives in `dictate.py`. Please keep it that way: small and easy to read.
