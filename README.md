# Vaani

**Siri for any laptop.** Vaani is a voice agent that runs in your terminal and is powered entirely by [Sarvam AI](https://sarvam.ai).
Speak in any of 11 Indian languages or Hinglish. Vaani operates your laptop and replies **out loud in your language**.
When something fails, it tells you why, also out loud and in your language.

Windows, macOS, Linux and WSL. Contributor notes: [HANDBOOK.md](HANDBOOK.md).

## Quick start

You need Python 3.10+ and a Sarvam API key from [dashboard.sarvam.ai](https://dashboard.sarvam.ai).

**Windows (PowerShell)**
```powershell
git clone https://github.com/srthk4370-IIITH/Vaani.git; cd Vaani; .\install.ps1
.\vaani.cmd          # or: .venv\Scripts\vaani
```
If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope Process Bypass` first.
`install.ps1` creates `.venv`, installs Vaani with its Windows extras, and runs onboarding.

**macOS / Linux / WSL**
```bash
git clone https://github.com/srthk4370-IIITH/Vaani.git; cd Vaani; ./install.sh
source .venv/bin/activate; vaani
```

**Manual**
```bash
python -m venv .venv
# activate it: .venv\Scripts\activate (Windows) or source .venv/bin/activate
pip install -e .        # on Windows: pip install -e ".[windows]"
vaani
```

**Onboarding** runs on first launch (or any time with `vaani setup`):
1. Sarvam API key, entered hidden and saved locally to `~/.vaani/.env`
2. Microphone test
3. Wake-word enrollment: say "Hey Vaani" 3 times
4. Push-to-talk key (default F9)
5. SAFE_MODE confirmations on or off
6. Reply language

## How to use

Run `vaani` (or `.\vaani.cmd`). The terminal UI shows:
- a **header** at the top,
- a scrolling **transcript** of what you said, the tools Vaani called, and its replies,
- a **toolbar** at the bottom with the current state: `idle — say Hey Vaani / hold F9`, `listening…` (with a level meter), `transcribing…`, `thinking…`, `speaking…`, or `confirm: … [y/N]` when an action needs your OK. It also shows whether safe mode and voice replies are on.

Give a command in any of three ways:
- say **"Hey Vaani, ..."**,
- **hold F9** while speaking, or
- **type** it at the prompt and press Enter.

**Slash commands**

| Command | What it does |
|---|---|
| `/help` | Show help |
| `/quit` (`/exit`, `/q`) | Exit (Ctrl+C / Ctrl+D also work) |
| `/clear` | Clear the screen |
| `/safe on\|off` | Toggle SAFE_MODE confirmations |
| `/lang hi-IN` | Reply language for typed commands (`auto` to reset) |
| `/voice on\|off` | Speak replies or mute TTS |

**Command line**

| Command | What it does |
|---|---|
| `vaani` | Starts the interactive terminal UI |
| `vaani --text "Notepad kholo" --lang hi-IN` | Runs one typed command, no mic. `--lang` sets the reply language |
| `vaani --no-voice` | Prints replies instead of speaking them |
| `vaani --new-window` | Opens Vaani in a new terminal window |
| `vaani --simple` | Plain loop without the interactive UI |
| `vaani doctor` | Checks the API key, Sarvam reachability, mic, audio output and wake word |
| `vaani setup` | Re-runs onboarding |
| `vaani --version` | Prints the version |

`-t` and `-l` are short for `--text` and `--lang`. Without installing, use `python -m vaani ...`.

## Architecture

```
 mic --(local wake word / F9)--> Saaras v4 STT --> Sarvam 105B orchestrator --> Bulbul v4-flash TTS --> speaker
                                 (translate mode,   (tool calling, max 6 steps)  (reply in user's language)
                                  language ID)           |
                                       +-----------------+------------------+
                                       |                                    |
                          laptop tools (local):               other Sarvam models as tools:
                          apps, URLs, keys, volume,           gemma4 vision, doc-ai, mayura,
                          clipboard, screenshot ...           transliterate, text-lid, 105B, bulbul
              every tool call --> guardrails (local rules + 105B risk review) --> audit log
```

1. **Hear:** `saaras:v4` in translate mode turns speech into English and detects the spoken language.
2. **Think and act:** `sarvam-105b` plans and picks tools. Some run locally on your laptop; others call another Sarvam model.
3. **Reply:** the answer is translated back and spoken with `bulbul:v4-flash`. Malayalam and Odia have no v4-flash voice, so those fall back to `bulbul:v3`.

| Sarvam model | Used for |
|---|---|
| `saaras:v4` | Speech-to-text, translate mode, language detection |
| `sarvam-105b` | Orchestrator agent (tool calling), cloud guardrail risk review, `ask_sarvam` |
| `gemma4` (vision) | `read_screen`, `read_image_text` |
| Sarvam Vision doc-ai | `read_document` |
| `mayura:v1` | `translate_text`, translating replies back to your language |
| Sarvam transliterate | `transliterate_text` |
| Sarvam text-lid | `detect_language` |
| `bulbul:v4-flash` / `bulbul:v3` | Spoken replies, `speak` |

**Cloud vs local.** All AI runs on Sarvam's cloud. Vaani ships no downloaded AI models.
Local: wake-word detection, execution of every action, onboarding, and storage in `~/.vaani`:
- `.env` holds your API key
- `settings.json` holds your preferences
- `history.jsonl` is the audit log of every tool call

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
| `read_document` | Digitise a PDF/PNG/JPG with Sarvam doc-ai; returns text or answers a question about it |
| `translate_text` | Translate between Indian languages and English (mayura) |
| `transliterate_text` | Convert script, e.g. Hinglish in Latin letters to Devanagari |
| `detect_language` | Identify the language and script of a text (text-lid) |
| `ask_sarvam` | Ask sarvam-105b a general-knowledge or writing question |
| `speak` | Say text aloud in a given language (bulbul) |
| `read_clipboard` | Read clipboard text |
| `get_time` | Get the current date and time |
| `wait` | Pause up to 5 s, e.g. while a page loads |
| `close_window` | Close the active window (asks you first) |
| `media_control` | Play/pause, next, previous, stop, mute toggle (playerctl on Linux) |
| `open_path` | Open a known folder (Downloads, Desktop ...), a path, or a file found by name; never runs executables |
| `system_info` | Battery % and charging, OS, CPU, RAM, free disk |
| `set_brightness` | Set laptop screen brightness 0-100 |
| `lock_screen` | Lock the screen (asks you first) |

## Example commands

PRD demo script:

| # | Command | Expected |
|---|---|---|
| 1 | "Chrome kholo aur YouTube pe lo-fi music search karo" | Browser opens YouTube search |
| 2 | Same command spoken in Telugu or Tamil | Same result, reply spoken in that language |
| 3 | "Notepad open chesi 'hello hackathon' ani type chey" | Opens Notepad and types |
| 4 | "Screen pe emi undi cheppu" | Screenshot + vision, spoken description |
| 5 | "Open an app called Zxqw" | Spoken error in user's language |

More:
- "Notepad kholo aur likho: kal meeting 10 baje"
- "ಈಗ ಸಮಯ ಎಷ್ಟು?" (What time is it?)
- "Volume 30 kar do"
- "Clipboard wala text Tamil mein translate karo"
- "Is PDF mein total amount kya hai?"

## Wake word and privacy

The wake word runs **entirely on your laptop**: a personalized "Hey Vaani" detector that compares your voice against the 3 samples you recorded during `vaani setup`, using MFCC features and DTW template matching in numpy.
It makes no API calls, uses no credits, and needs no downloaded AI model.
Audio goes to Saaras only after a local wake-word match, or while you hold F9. Everything else stays on the device.
To re-enroll, run `vaani setup`. To use push-to-talk only, set `VAANI_WAKE=0`.

## Guardrails and safety

Every tool call passes through `vaani/guardrails.py` before it runs.

- **Local rules come first.**
  - Nothing gets deleted, uninstalled or killed unless your command explicitly asks for it.
  - Dangerous strings are blocked from `type_text`, e.g. `rm -rf`, `format C:`, `del /s`, `shutdown`, `iex`, `curl | sh`, `diskpart` and `reg delete`.
  - Destructive hotkeys are blocked or confirmed: Alt+F4, Shift+Delete, Cmd+Q and similar.
- **You confirm first** before Vaani sends, pays, orders, submits, presses Enter, or closes a window.
- **Cloud AI risk review:** for ambiguous actions, `sarvam-105b` judges the risk. If the review is unavailable, Vaani asks you instead. `VAANI_CLOUD_GUARD=0` turns it off.
- **SAFE_MODE** is on by default. `VAANI_SAFE_MODE=0` (or `/safe off`) skips the confirmation prompts; block rules still apply.
- **Abort at any time:** pyautogui FAILSAFE is on, so slamming the mouse into a screen corner stops all automation.
- **Audit log:** every decision and result is appended to `~/.vaani/history.jsonl`.
- **No shell, ever.** `open_app` launches apps by name only, and Vaani never runs model-generated shell commands. Tool errors are spoken back and never crash the loop.

## Configuration

Set these in `~/.vaani/.env` or in your environment.

| Variable | Default | Purpose |
|---|---|---|
| `SARVAM_API_KEY` | (none) | Sarvam API subscription key |
| `VAANI_HOME` | `~/.vaani` | Config, logs and temp directory |
| `VAANI_PTT_KEY` | `f9` | Push-to-talk key |
| `VAANI_WAKE` | `1` | `0` turns off the wake word (push-to-talk only) |
| `VAANI_WAKE_WORD` | `hey vaani` | Wake phrase label |
| `VAANI_SAFE_MODE` | `1` | `0` skips the confirmation prompts |
| `VAANI_CLOUD_GUARD` | `1` | `0` turns off the 105B risk review |
| `VAANI_AGENT_MODEL` | `sarvam-105b` | Orchestrator LLM |
| `VAANI_FALLBACK_MODEL` | `sarvam-105b` | Fallback LLM |
| `VAANI_KNOWLEDGE_MODEL` | `sarvam-105b` | Model behind `ask_sarvam` |
| `VAANI_VISION_MODEL` | `gemma4` | Vision model |
| `VAANI_REASONING_EFFORT` | (off) | `low` / `medium` / `high` / `max` |
| `VAANI_STT_MODEL` / `VAANI_STT_TRANSLATE_MODEL` | `saaras:v4` | Speech-to-text |
| `VAANI_TRANSLATE_MODEL` | `mayura:v1` | Translation |
| `VAANI_TTS_MODEL` | `bulbul:v4-flash` | Text-to-speech |
| `VAANI_TTS_SPEAKER` | (per language) | Force one TTS voice |

## Troubleshooting

- **No mic input or the wrong device:** run `vaani doctor` and set your default input device in OS settings.
  - Windows: Settings > Privacy > Microphone > "Let desktop apps access your microphone".
  - macOS: System Settings > Privacy & Security > Microphone, then allow your terminal.
- **Wake word misses or false-triggers:** re-run `vaani setup` to re-record your 3 samples in your usual environment, or hold F9.
- **macOS: keys or typing do nothing:** grant your terminal **Accessibility** access (and Input Monitoring if F9 does nothing).
- **WSL: no audio:** WSL has no direct mic access without WSLg/PulseAudio. Use `--text`, or run Vaani natively on Windows.
- **Linux Wayland: hotkeys or typing fail:** global key hooks and pyautogui need X11. Use an Xorg session, the wake word, or `--text`.
- **Linux audio/automation deps:** `sudo apt install libportaudio2 python3-tk python3-dev scrot xclip`.
- **Vision returns 403:** `gemma4` is in beta on Sarvam's v2 chat endpoint and your key needs beta access. Everything else still works.
- **401 / invalid key:** run `vaani setup` again.

## Verified status

Live-tested on Windows, October 2026:
- PRD demos 1, 3, 4 and 5 pass end to end.
- Tool selection verified for `detect_language`, `transliterate_text` and `read_document`.
- Sarvam doc-ai reads a PDF correctly through `read_document`.

Not yet verified:
- PRD demo 2 by voice (Telugu/Tamil speech).
- Runtime on macOS and Linux (installers written, not run on real hardware).
