# dialog-dictate

Push-to-talk dictation for macOS, powered by PolyAI's Dialog-RSN-1.

Hold **Right Option** (⌥ on the right of the space bar), speak, then release. Your words are typed into whatever app you're using: Slack, Chrome, your editor, anywhere you can paste.

## What you need

- A Mac
- Your own PolyAI API key
- [uv](https://docs.astral.sh/uv/), a Python tool installer. If you don't have it, run `brew install uv`.
- Access to this GitHub repo

You don't need to install Python yourself. uv takes care of it.

## Setup

**1. Install**

```sh
uv tool install git+ssh://git@github.com/polyai/dialog-dictate
```

If your terminal then says `dictate: command not found`, run `uv tool update-shell` and open a new terminal window.

**2. Add your API key**

```sh
dictate setup
```

Paste your key when asked. It won't be shown as you type. Setup checks the key, works out which region it belongs to, and saves it securely in your Mac's Keychain. You only do this once.

**3. Start dictating**

```sh
dictate
```

Leave this running in a terminal window. Hold Right Option, speak, release, and the text appears where your cursor is. Press Ctrl+C in the terminal to stop.

**4. Grant permissions (first run only)**

macOS will ask for some permissions the first time. They're granted to **your terminal app** (Terminal, iTerm, Warp, Ghostty, etc.), not to `dictate` itself. If you miss a prompt, turn them on manually in **System Settings → Privacy & Security**:

| Permission       | What it's for                    | If it's missing                                  |
|------------------|----------------------------------|--------------------------------------------------|
| Microphone       | Hearing you                      | Transcripts come back empty                      |
| Input Monitoring | Noticing when you hold the key   | Nothing happens when you hold Right Option       |
| Accessibility    | Pasting the text into your app   | Text is copied but never appears                 |

**Quit and reopen your terminal after changing these**, because macOS only applies them on restart. `dictate` warns you at startup if Input Monitoring or Accessibility is missing.

## Good to know

- **Your clipboard is replaced.** Dictated text is pasted via the clipboard, so whatever you'd copied before is overwritten.
- **Audio is sent to PolyAI's API** for transcription, using your own key.
- **Updating:** `uv tool upgrade dialog-dictate`
- **Uninstalling:** `uv tool uninstall dialog-dictate`. Your saved key stays in Keychain (under `dialog-dictate`) until you delete it in Keychain Access.

## API keys and regions

Everyone uses their own key. Please don't share keys or paste them into Slack.

Each key belongs to one PolyAI region and only works there. You don't need to know which region yours is: `dictate setup` works it out for you. If your key is rotated or revoked, `dictate` will tell you; just run `dictate setup` again.

<details>
<summary>Details for the curious</summary>

- The key is stored in your login Keychain under the service `dialog-dictate`.
- The detected region is saved in `~/.config/dialog-dictate/config.json`.
- Setup tries each known endpoint and keeps the first one that accepts your key:

  | Region   | Endpoint                               |
  |----------|----------------------------------------|
  | `studio` | `wss://api.studio.poly.ai/v1/realtime` |
  | `us`     | `wss://api.us.poly.ai/v1/realtime`     |

- The `POLY_API_KEY` and `DIALOG_REALTIME_URL` environment variables override the saved key and region. To use an endpoint not listed above, run `DIALOG_REALTIME_URL=wss://... dictate setup`.

</details>

## Troubleshooting

| Problem                                 | Fix                                                                                                  |
|-----------------------------------------|------------------------------------------------------------------------------------------------------|
| `dictate: command not found`            | Run `uv tool update-shell`, then open a new terminal window.                                         |
| "No API key found"                      | Run `dictate setup`.                                                                                 |
| "Key rejected"                          | Your key has changed or was revoked. Run `dictate setup` with a current key.                         |
| Nothing happens when holding the key    | Check Input Monitoring for your terminal app, then quit and reopen the terminal.                     |
| Text doesn't appear after speaking      | Check Accessibility for your terminal app, then quit and reopen the terminal.                        |
| Transcripts are empty                   | Check Microphone for your terminal app, and that the right input is selected in Sound settings.      |

Still stuck? Run `dictate --debug` and share the output with whoever maintains this repo. Debug mode logs every key you press, so **don't type passwords while it's on**, and check the output before sharing it.

## Other options

```sh
dictate --file recording.wav   # transcribe a WAV file (16 kHz, mono, 16-bit)
dictate --debug                # show what's happening behind the scenes
```

## Contributing

```sh
git clone git@github.com:polyai/dialog-dictate
cd dialog-dictate
uv tool install -e .   # `dictate` now runs your local copy; edits apply immediately
```
