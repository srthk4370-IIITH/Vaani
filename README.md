# Vaani

**Siri for any laptop.** Vaani is a voice agent that runs in your terminal and is powered entirely by [Sarvam AI](https://sarvam.ai).
Speak in any of 11 Indian languages or Hinglish. Vaani operates your laptop and replies **out loud in your language**.
When something fails, it tells you why, also out loud and in your language.

Windows, macOS, Linux and WSL. Contributor notes: [HANDBOOK.md](HANDBOOK.md).

## Architecture

```
 mic --(local VAD / F9)--> Saaras v4 STT ----> Sarvam 105B orchestrator ----> Bulbul v4-flash TTS --> speaker
                           (translate mode,     (tool calling, max 6 steps)     (reply in user's language)
                            auto language ID)        |
                                                     +--> local tools: apps, URLs, keys, volume, clipboard ...
                                                     +--> Sarvam models as tools: gemma4, mayura, transliterate,
                                                          text-lid, sarvam-105b, bulbul
                                   every tool call --> guardrails (local rules + 105B risk review) --> audit log
```

1. **Hear:** `saaras:v4` in translate mode turns speech into English and detects the spoken language.
2. **Think and act:** `sarvam-105b` plans and calls tools. When it needs another Sarvam model, it calls that model as a tool.
3. **Reply:** the answer is translated back and spoken with `bulbul:v4-flash`. Malayalam and Odia have no v4-flash voice, so those fall back to `bulbul:v3`.

| Sarvam model | Used for |
|---|---|
| `saaras:v4` | Speech-to-text, translate mode, language detection, wake-word check |
| `sarvam-105b` | Orchestrator agent (tool calling), cloud guardrail risk review, `ask_sarvam` |
| `gemma4` (v2 chat, vision) | `read_screen`, `read_image_text` |
| `mayura:v1` | `translate_text`, translating replies back to your language |
| Sarvam transliterate | `transliterate_text` |
| Sarvam text-lid | `detect_language` |
| `bulbul:v4-flash` / `bulbul:v3` | Spoken replies, `speak` |

**Cloud vs local.** All AI runs on Sarvam's cloud. Vaani ships **no local AI models**.
These stay on your machine: execution of every action, onboarding, and storage in `~/.vaani`:
- `.env` holds your API key
- `settings.json` holds your preferences
- `history.jsonl` is the audit log of every tool call

## Quick start

You need Python 3.10+ and a Sarvam API key.

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
# or manually:
py -3 -m pip install -e ".[windows]"
vaani setup
```

**macOS / Linux / WSL**
```bash
chmod +x install.sh && ./install.sh
source .venv/bin/activate && vaani setup
```

`vaani setup` asks for your API key (input is hidden) and saves it to `~/.vaani/.env`.
If you write `.env` yourself with PowerShell `echo ... > .env`, the file comes out as UTF-16. Vaani reads that encoding too.

## Usage

| Command | What it does |
|---|---|
| `vaani` | Starts listening. Say **"Hey Vaani, ..."** or **hold F9** and speak |
| `vaani --text "Notepad kholo" --lang hi-IN` | Typed command, no mic. `--lang` sets the reply language |
| `vaani --no-voice` | Prints replies instead of speaking them (combine with the other flags) |
| `vaani doctor` | Checks the API key, Sarvam reachability, mic, audio output and wake-word mode |
| `vaani setup` | Onboarding: API key, mic test, wake-word opt-in, preferences |
| `py -3 -m vaani ...` | Runs without installing (`python3 -m vaani` on macOS/Linux) |

## Capabilities

| Tool | What it does |
|---|---|
| `open_app` | Launch an app by name (Chrome, Notepad, VS Code, Spotify, Settings ...) |
| `open_url` | Open a URL in the default browser |
| `search_web` | Search Google or YouTube |
| `type_text` | Type into the focused window, optionally pressing Enter |
| `press_key` | Press one key (enter, esc, tab, arrows, playpause, volumemute ...) |
| `hotkey` | Press a key combination, e.g. Ctrl+T |
| `scroll` | Scroll the active window |
| `set_volume` | Set system volume from 0 to 100 |
| `screenshot` | Save a screenshot |
| `read_screen` | Answer questions about what is on screen (gemma4 vision) |
| `read_image_text` | Read or describe text in an image file (gemma4 vision) |
| `read_document` | Digitise a PDF/PNG/JPG with Sarvam Vision doc-ai; returns text or answers a question about it |
| `translate_text` | Translate between Indian languages and English (mayura) |
| `transliterate_text` | Convert script, e.g. Hinglish in Latin letters to Devanagari |
| `detect_language` | Identify the language and script of a text (text-lid) |
| `ask_sarvam` | Ask sarvam-105b a general-knowledge or writing question |
| `speak` | Say text aloud in a given language (bulbul) |
| `read_clipboard` | Read clipboard text |
| `get_time` | Get the current date and time |
| `wait` | Pause up to 5 s, e.g. while a page loads |
| `close_window` | Close the active window (asks you first) |

## Example commands

PRD demo script (1, 3, 4, 5 live-verified via `--text`):

| # | Command | Expected |
|---|---|---|
| 1 | "Chrome kholo aur YouTube pe lo-fi music search karo" | Browser opens YouTube search |
| 2 | Same command in Telugu or Tamil | Same result, reply spoken in that language |
| 3 | "Notepad open chesi 'hello hackathon' ani type chey" | Opens Notepad and types |
| 4 | "Screen pe emi undi cheppu" | Screenshot + vision, spoken description |
| 5 | "Open an app called Zxqw" | Spoken error in user's language |

More:

- "Hey Vaani, YouTube kholo aur lo-fi music search karo"
- "Notepad kholo aur likho: kal meeting 10 baje"
- "Screen pe kya hai batao"
- "ಈಗ ಸಮಯ ಎಷ್ಟು?" (What time is it?)
- "Google pe aaj ka weather search karo"
- "Volume 30 kar do"
- "Clipboard wala text translate this to Tamil"
- "Is image mein kya likha hai padh do"

## Guardrails and safety

Every tool call passes through `vaani/guardrails.py` before it runs.

- **Local rules come first.**
  - Nothing gets deleted, uninstalled or killed unless your command explicitly asks for it.
  - Dangerous strings are blocked from `type_text`, e.g. `rm -rf`, `format C:`, `del /s`, `shutdown`, `iex`, `curl | sh`, `diskpart` and `reg delete`.
  - Destructive hotkeys are blocked or confirmed: Alt+F4, Shift+Delete, Cmd+Q and similar.
- **You confirm first** before Vaani sends, pays, orders, submits, presses Enter, or closes a window.
- **Cloud AI risk review:** for ambiguous actions, `sarvam-105b` judges the risk. If the review is unavailable, Vaani asks you instead. `VAANI_CLOUD_GUARD=0` turns the review off.
- **SAFE_MODE** is on by default. `VAANI_SAFE_MODE=0` skips the confirmation prompts, but the block rules still apply.
- **Abort at any time:** pyautogui FAILSAFE is on, so slamming the mouse into a screen corner stops all automation.
- **Audit log:** every decision and result is appended to `~/.vaani/history.jsonl`.
- **No shell, ever.** `open_app` launches apps by name only, and Vaani never runs model-generated shell commands. Tool errors are spoken back to you and never crash the loop.

## Wake word and privacy

Voice-activity detection runs locally and is a simple energy threshold, not an AI model.
With the wake word on, each spoken phrase is sent to Saaras to check whether it contains "Hey Vaani".
Phrases without the wake word are discarded, but that audio still leaves your device and uses API credits.
Push-to-talk (hold F9) sends audio only while you hold the key.
To disable the wake word, run `vaani setup` or set `VAANI_WAKE=0`.

## Configuration

Set these in `~/.vaani/.env` or in your environment.

| Variable | Default | Purpose |
|---|---|---|
| `SARVAM_API_KEY` | (none) | Sarvam API subscription key |
| `VAANI_HOME` | `~/.vaani` | Config, logs and temp directory |
| `VAANI_PTT_KEY` | `f9` | Push-to-talk key |
| `VAANI_WAKE` | `1` | `0` turns off the wake word (push-to-talk only) |
| `VAANI_WAKE_WORD` | `hey vaani` | Wake phrase |
| `VAANI_SAFE_MODE` | `1` | `0` skips the confirmation prompts |
| `VAANI_CLOUD_GUARD` | `1` | `0` turns off the 105B risk review |
| `VAANI_AGENT_MODEL` | `sarvam-105b` | Orchestrator LLM |
| `VAANI_FALLBACK_MODEL` | `sarvam-105b` | Fallback LLM |
| `VAANI_VISION_MODEL` | `gemma4` | Vision model |
| `VAANI_REASONING_EFFORT` | (off) | `low` / `medium` / `high` / `max` |
| `VAANI_STT_MODEL` / `VAANI_STT_TRANSLATE_MODEL` | `saaras:v4` | Speech-to-text |
| `VAANI_TRANSLATE_MODEL` | `mayura:v1` | Translation (1000 chars; `sarvam-translate:v1` allows 2000) |
| `VAANI_TTS_MODEL` | `bulbul:v4-flash` | Text-to-speech |
| `VAANI_TTS_SPEAKER` | (per language) | Force one TTS voice |

## Troubleshooting

- **No mic input or the wrong device:** run `vaani doctor` and set your default input device in OS settings.
  - Windows: Settings > Privacy > Microphone > "Let desktop apps access your microphone".
  - macOS: System Settings > Privacy & Security > Microphone, then allow your terminal.
- **macOS: keys or typing do nothing:** grant your terminal **Accessibility** access (and Input Monitoring if F9 does nothing).
- **WSL: no audio:** WSL has no direct mic access without WSLg/PulseAudio. Use `--text`, or run Vaani natively on Windows.
- **Linux Wayland: hotkeys or typing fail:** global key hooks and pyautogui need X11. Log into an Xorg session, or use the wake word or `--text`.
- **Vision returns 403:** `gemma4` is in beta on Sarvam's v2 chat endpoint, and your key needs beta access. Every other feature still works.
- **401 / invalid key:** run `vaani setup` again.

## Tested status

Live-verified on Windows in October 2026:
- Working: STT, translate, TTS, 105B tool calls and gemma4 vision.
- PRD demos 1, 3 and 5 pass end to end.
- Typical agent latency is about 3-6 s per command.
