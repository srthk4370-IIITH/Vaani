# Vaani Handbook

## What we are building

**Vaani** is a Siri-style voice assistant for any laptop (Windows, macOS, Linux/WSL), run from the terminal,
powered by Sarvam AI. Speak in any Indian language or Hinglish; Vaani understands, operates your laptop
(open apps, search the web, type, press keys, scroll, volume, read the screen) and answers back **out loud in
your language**. If something fails, it explains the failure aloud, also in your language.

Source spec: `Vaani_PRD.pdf`. Sarvam model/endpoint reference: `model info/` screenshots + docs.sarvam.ai.

### Design principles
- **Local-first, no local AI models.** Onboarding, settings, API key storage, voice-activity detection (energy
  threshold), wake-word spotting (numpy MFCC + DTW vs. the user's own 3 enrolled samples), audio record/playback,
  and all laptop control run on-device. AI calls (STT of the command, translate, TTS, LLM agent, vision) go to Sarvam's cloud.
- **Two ways to activate:** wake word **"Hey Vaani"** (local energy VAD captures each utterance -> local MFCC+DTW
  match against enrolled templates in ~/.vaani/wake_templates.npz; non-matches silently ignored with NO API call;
  only the command after the wake phrase is sent to STT) and
  **hold F9** push-to-talk (configurable).
- **Safe.** Whitelisted tools only, no shell execution of model strings, SAFE_MODE confirmation before
  Enter/send/delete/purchase, `pyautogui.FAILSAFE` on, `execute_tool` never raises.
- **Key is asked during onboarding** (`vaani setup`), stored in `~/.vaani/.env`, never committed.

### Flow
```
[hold F9] -> record 16 kHz mono WAV (local)
[wake] local VAD utterance -> local MFCC+DTW match? no: ignore (no API) | yes+command: trim WAV after wake -> STT | yes only: beep, record -> STT
  -> Sarvam STT (saaras:v4, mode=translate)  -> English text + detected language
  -> Sarvam 105B agent (tool-calling loop, max 6 steps)
       laptop tools: open_app, open_url, search_web, type_text, press_key, hotkey, scroll,
                     set_volume, screenshot, read_clipboard, get_time, wait, close_window
       Sarvam tools: read_screen (vision), translate_text, speak, transliterate_text,
                     detect_language (text-lid), read_image_text (gemma4 OCR), read_document (doc-ai digitise PDF/PNG/JPG + 105b Q&A), ask_sarvam (105b knowledge)
  -> final English reply -> translate to user's language -> Bulbul TTS -> play (local)
  errors -> friendly sentence -> translated -> spoken; Sarvam down -> local beep
```

### Layout
```
vaani/
  config.py         models, endpoints, constants, paths (~/.vaani), SAFE_MODE
  sarvam_client.py  stt_translate, translate, tts, chat, vision_read, ping  (SarvamError)
  audio_io.py       record_while_key_held, record_until_silence, play_audio, beep, mic_check
  wakeword.py       local KWS: enroll()/matches() numpy MFCC+DTW; contains_wake() strips wake text
  trigger.py        wait_for_command(): wake word OR PTT, returns wav path
  tools.py          TOOL_SCHEMAS, execute_tool(name, args) -> {"ok","result"}
  agent.py          run_agent(command_en, lang, on_event) -> str
  onboarding.py     `vaani setup`: API key, mic test, wake-word enrollment (3 samples + verify), preferences
  main.py           CLI + rich live console + main loop + error voice-back
HANDBOOK.md, README.md, pyproject.toml, install.sh, install.ps1, .gitignore
```

### Verified Sarvam config (docs.sarvam.ai, Oct 2026)
| Purpose | Model | Endpoint |
|---|---|---|
| STT + translate | `saaras:v4` (`mode="translate"`) | `POST /speech-to-text` |
| Translate | `mayura:v1` (1000 chars) | `POST /translate` |
| TTS | `bulbul:v4-flash` (per-lang speakers), fallback `bulbul:v3` for ml/od | `POST /text-to-speech` |
| Agent | `sarvam-105b` (tool calling, reasoning off) | `POST /v1/chat/completions` |
| Vision | `gemma4` (image input, v2 beta) | `POST /v2/chat/completions` |

Auth header: `api-subscription-key: <key>`.

## Status (live log)

| Track | Owner | Status |
|---|---|---|
| config.py | main | ✅ done, models verified by client agent |
| sarvam_client.py | agent B | ✅ done (docs-verified, untested live) |
| audio_io.py, wakeword.py, trigger.py | agent A | ✅ done (energy VAD + local MFCC/DTW wake spotting, no model, no API) |
| tools.py | agent C | ✅ done, tested on Windows (16 tools) |
| agent.py | agent D | ✅ done, offline-tested (native+JSON fallback) |
| main.py, onboarding.py | agent E | ✅ done (help/doctor tested offline) |
| packaging (pyproject, install scripts, README, .gitignore) | agent F | ✅ done |
| deps install (pip) | background | ✅ done (py3.14) |
| Vosk wake model download | - | ❌ removed per user: no local AI models |
| Local wake word (MFCC + DTW, personalized enrollment; `vaani setup --wake`) | agent A + main | ✅ done |
| guardrails.py (local rules + Sarvam AI risk review + audit log) | guard agent | ✅ done, offline-tested |
| Terminal UI (ui.py: Codex-style transcript, prompt_toolkit toolbar w/ live state + mic meter, typed+voice queue, slash cmds, in-prompt SAFE_MODE confirm, rich fallback, `--new-window`, `--simple`) | UI agent | ✅ done (render-tested; interactive untested) |

## Guardrails (`vaani/guardrails.py`)
All AI stays on Sarvam cloud; execution + storage are local. Every `tools.execute_tool(name, args, user_command)` call goes through `guardrails.check()` first:
- **Layer 1, local rules (instant):** read-only tools (screenshot, read_screen, clipboard, time, translate, speak, scroll, wait, search_web, http(s) open_url) pass immediately. Delete/uninstall/kill/Shift+Delete/emptying trash is **blocked** unless the user's command explicitly says delete/remove/erase/hatao/mitao/uninstall, and even then needs confirmation. Typing shell-dangerous strings (`rm -rf`, `format c:`, `del /s`, `shutdown`, `reg delete`, `powershell -enc`, `curl|sh`, ...) is always blocked. Pay/buy/send/submit/post text, Enter after typing, and close_window/Alt+F4 need confirmation (Enter/close follow SAFE_MODE).
- **Layer 2, Sarvam AI review:** actions the rules didn't decide (e.g. ordinary type_text, hotkeys, open_app) go to `sarvam_client.chat` (AGENT_MODEL), which returns low/medium/high risk. high = block, medium = confirm, low = allow. Results are cached per process. On timeout (10s) or error: confirm if SAFE_MODE, otherwise allow. Turn off with `VAANI_CLOUD_GUARD=0`.
- Tool-level SAFE_MODE confirms stay in place (defense in depth). If the guardrail already got a yes, the tool doesn't ask again.
- **Audit:** every call and its result is appended to `~/.vaani/history.jsonl`.
- The agent system prompt also says: never delete, uninstall or remove anything unless the user explicitly asked to.

## Still to do
- [ ] Integrate all modules; `py -m vaani --text "open notepad"` works end to end (with key)
- [ ] Offline smoke tests (no key): imports, `execute_tool` safety, wake VAD init, `contains_wake` + MFCC/DTW `matches` on TTS audio, onboarding flow
- [ ] User provides Sarvam API key -> `vaani setup` -> run 5 demo commands (PRD §8)
- [ ] Tune VAD threshold / min-max utterance length live; verify "Hey Vaani" survives STT (Vani/Vaani/Wani)
- [ ] Optionally `pip uninstall vosk` and delete old `~/.vaani/models` on machines that ran earlier builds
- [ ] Commit + push to `origin` (github.com/srthk4370-IIITH/Vaani) with `.env`, `~/.vaani`, tmp ignored

## Demo commands (acceptance)
1. "Chrome kholo aur YouTube pe lo-fi music search karo" -> browser search
2. Same in Telugu/Tamil -> same result, reply in that language
3. "Notepad open chesi 'hello hackathon' ani type chey" -> opens and types
4. "Screen pe emi undi cheppu" -> screenshot + vision, spoken description
5. "Open an app called Zxqw" -> spoken error in user's language

## Usage (target)
```
pip install -e .        # or ./install.sh  /  .\install.ps1
vaani setup             # asks for Sarvam API key, mic test, wake word enrollment (`vaani setup --wake` re-enrolls)
vaani                   # say "Hey Vaani ..." or hold F9
vaani --text "open notepad"   # typed command, no mic
```

## Known uncertainties (verify once key arrives)
Docs snapshot: `sarvam_docs/` (gitignored), fetched 2026-10-17.
- RESOLVED: TTS field is `language_code` (required); `target_language_code` not in schema -> removed. (docs.sarvam.ai/api-reference/text-to-speech/convert)
- RESOLVED: `saaras:v4` supports `mode=translate` ("available on both saaras:v3 and saaras:v4", docs.sarvam.ai/api/getting-started/models/saaras). API-ref text still says "only saaras:v3" (stale); if rejected, `VAANI_STT_TRANSLATE_MODEL=saaras:v3`.
- RESOLVED: v4-flash speaker IDs in config all match verbatim (docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/voices). v4-flash catalog has no ml-IN/od-IN -> bulbul:v3 `shubh` fallback is correct. Default v4 speaker if omitted: `shubh_enhi_ads`.
- RESOLVED: Vision `gemma4` is v2-only (`/v2/chat/completions`, beta access per key); images as `{"type":"image_url","image_url":{"url":"data:image/...;base64,..."}}` (remote URLs rejected). v1 accepts only `sarvam-105b`/`sarvam-105b-conversations`; sarvam-105b is text-only -> v1 fallback removed, clear error on 401/403/404. (api-reference/chat/chat-completions-v2, models/openweight/gemma-4-31b)
- RESOLVED: Chat tools are OpenAI-compatible: `tools` [{type:function,function:{name,description,parameters}}], `tool_choice`, response `choices[0].message.tool_calls` (stringified JSON `arguments`), `finish_reason:"tool_calls"`, reply with `{"role":"tool","tool_call_id","content"}`. `content` may be null when tool_calls present; optional `message.reasoning_content`. `reasoning_effort: null` disables reasoning (v1 ref). `max_tokens` default 2048. (api-reference/chat/chat-completions-v1, chat-completion/overview)
- OPEN: v2 auth failures return 403 (not 401); live behaviour of keys without beta access unverified.
- macOS/Linux/WSL tool paths written, only Windows tested.
