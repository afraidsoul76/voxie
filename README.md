# 🎙️ voxie

A voice-controlled desktop assistant powered by Claude. **Speak — voxie sees your screen, decides what to do, and does it.**

> _Work in progress — README, demo GIF, and screenshots filled in after the first end-to-end run._

## What it does

Press a hotkey, speak a command in natural language, and voxie:

1. **Transcribes** your voice locally with Whisper (no audio leaves your machine)
2. **Sees your screen** — takes a screenshot and sends it to Claude Sonnet 4.5 (with vision)
3. **Decides** what to do via Claude's tool-use: open an app, click a UI element by name, type text, run a shell command, focus a window, press a hotkey
4. **Does it** with pyautogui + pygetwindow

Examples:

- *"open my usb-vault project in VS Code"* → launches Code, focuses it
- *"click the sign up button"* → screenshots the current window, Claude returns pixel coordinates, pyautogui clicks
- *"open Chrome and search for pyodide docs"* → launches Chrome, focuses address bar, types the query, presses Enter
- *"what windows do I have open right now?"* → lists window titles, speaks a summary

## Stack

- **Voice:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with `base.en` model — 100% local, ~150 MB one-time download
- **LLM:** Claude Sonnet 4.5 via `@anthropic-ai/sdk` (Python) — supports base URL overrides for API-compatible proxies
- **Vision:** screenshot → PNG → Anthropic vision input; coordinates rescaled from what-Claude-saw back to real screen pixels
- **Desktop control:** `pyautogui` (mouse/keyboard), `pygetwindow` (window management), `subprocess` (shell + `start` command)
- **Screen capture:** `mss` (fast, DPI-aware)
- **UI:** `pystray` system tray (color-coded state: gray = idle, red = listening, amber = thinking, green = acting) + Tk overlay showing live transcript + action trace
- **Hotkey:** `pynput` global hotkey (default `Ctrl+Alt+Space`)

## Local dev

Windows-first (that's where I run it). Should mostly work on macOS/Linux with some tweaks around `open_app` and screenshot DPI, but not tested.

```bash
git clone https://github.com/afraidsoul76/voxie.git
cd voxie
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# fill in ANTHROPIC_API_KEY

python -m voxie
# or double-click voxie.bat for a no-console launch
```

First run downloads the Whisper model (~150 MB, one-time).

## How it fits together

```
                                                Anthropic API
                                                     ↑↓
                                                  (vision +
                                                   tool-use)
                                                     |
   hotkey  ─►  Recorder ─► Whisper ─► transcript ─► Assistant ─► tools ─► pyautogui / subprocess / pygetwindow
    ▲                                                                                     |
    │                                                                                     ▼
   Tray (idle/listening/thinking/acting)  ◄────  Overlay (transcript + action trace)  ◄─── your desktop
```

Every tool result is fed back to Claude as a `tool_result` block, so it can chain multi-step operations (open → focus → click → type) without you having to script them.

## Tools voxie can call

| Tool | What it does | Needs vision? |
|---|---|---|
| `open_app` | Launch by name via Windows `start` — with aliases for common apps | – |
| `focus_window` | Bring the first window whose title matches to front | – |
| `list_windows` | Report all visible window titles back to Claude | – |
| `click_xy` | Click at coords (rescaled from screenshot dimensions to real screen) | ✓ |
| `type_text` | Type text at the current keyboard focus | – |
| `press_key` | Press a key or hotkey combo (`enter`, `ctrl+t`, `alt+tab`, …) | – |
| `open_url` | Open a URL in the default browser | – |
| `run_shell` | Run a shell command via `cmd`. Destructive commands (rm/del/shutdown/kill) require explicit confirmation | – |

## Security + privacy notes

Important, because voice control of a desktop is a genuinely spicy attack surface.

- **Whisper runs locally.** Your audio never leaves your machine.
- **Screenshots go to Anthropic.** The Assistant sends a screenshot of your primary display along with your transcript. If sensitive info is visible, it'll be visible to the model. Consider hiding password managers, private DMs, etc. before running voxie.
- **Shell commands are gated.** Anything matching a destructive-keyword pattern (`rm`, `del`, `shutdown`, `format`, `kill`, `taskkill`, output redirection, `sudo`/`runas`) refuses to run unless the model passes `confirmed=true`. The system prompt tells Claude to only pass that after the user says yes out loud.
- **pyautogui failsafe.** Slamming your cursor into a screen corner aborts pyautogui immediately — panic button if voxie starts doing something wrong.
- **Wake-word style:** currently push-to-talk only. No always-listening. If you leave the hotkey alone, voxie doesn't record.
- **Your API key.** Lives in `.env`, never committed. Rotate if you ever share your screen.

## Known limitations

- **Windows-first.** `open_app` uses `cmd`'s `start`; `pygetwindow` is Windows-focused. Cross-platform port would work but isn't done.
- **Screenshot cost.** Every command with vision costs a Claude Sonnet vision call (~$0.003–$0.01 depending on screen size). Voxie could easily rack up $1–2/hour of heavy use.
- **Model latency.** Sonnet 4.5 vision calls take 2–5 seconds. Not instant. Fine for "open Chrome"; less magical for "click that button" if you're already halfway to clicking it manually.
- **Precise clicking on small UI.** The screenshot is downsampled to ≤1568px on the long side (Anthropic's recommendation for vision). Very tiny UI elements (menu bar icons, sub-pixel controls) may be misidentified.
- **No wake-word.** Push-to-talk keeps things simple + safe; adding an always-listening wake-word ("hey voxie") would need something like Picovoice Porcupine.

## License

MIT — see [LICENSE](LICENSE).
