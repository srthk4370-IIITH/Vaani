"""Vaani CLI: main voice loop, typed mode, setup, doctor. Error voice-back in user's language."""
import argparse
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from . import __version__, config, onboarding

console = Console()


# ---------- helpers ----------
def _timer():
    t0 = time.perf_counter()
    return lambda: time.perf_counter() - t0


def _dim(label: str, secs: float) -> None:
    console.print(f"[dim]  {label}: {secs:.2f}s[/]")


def _short(obj, n: int = 160) -> str:
    s = str(obj).replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _on_event(kind: str, data: dict) -> None:
    try:
        if kind == "tool_call":
            console.print(f"  [cyan]> {data.get('name')}[/] [dim]{_short(data.get('args', {}))}[/]")
        elif kind == "tool_result":
            mark = "[green]ok[/]" if data.get("ok") else "[red]fail[/]"
            console.print(f"    {mark} [dim]{_short(data.get('result', ''))}[/]")
        else:
            console.print(f"  [dim]{kind}: {_short(data)}[/]")
    except Exception:
        pass


def _confirm(question: str) -> bool:
    try:
        return Confirm.ask(f"[yellow]Vaani wants to:[/] {question}", default=False)
    except (EOFError, KeyboardInterrupt):
        return False


def _to_lang(text: str, lang: str) -> str:
    if not lang or lang == "en-IN":
        return text
    from . import sarvam_client
    return sarvam_client.translate(text, "en-IN", lang)


def _speak(text: str, lang: str, voice: bool) -> None:
    if not voice:
        return
    from . import audio_io, sarvam_client
    t = _timer()
    audio = sarvam_client.tts(text, lang or "en-IN")
    _dim("tts", t())
    audio_io.play_audio(audio)


def _friendly_error(e: Exception) -> str:
    try:
        from .sarvam_client import SarvamError
        if isinstance(e, SarvamError):
            return "Sorry, I couldn't reach Sarvam right now. Please check your internet or API key."
    except Exception:
        pass
    return f"Sorry, something went wrong: {_short(e, 100)}"


def _voice_back_error(e: Exception, lang: str, voice: bool) -> None:
    msg = _friendly_error(e)
    console.print(f"[red]Error:[/] {_short(e, 300)}")
    try:
        local = _to_lang(msg, lang)
        console.print(f"[bold magenta]Vaani:[/] {local}")
        _speak(local, lang, voice)
    except Exception as e2:
        console.print(f"[red]Could not speak the error ({_short(e2, 120)}).[/]")
        try:
            from . import audio_io
            audio_io.beep("error")
        except Exception:
            pass


def _handle_command(command_en: str, lang: str, voice: bool) -> None:
    from . import agent
    t = _timer()
    console.print("[bold]Thinking...[/]")
    reply = agent.run_agent(command_en, lang, _on_event)
    _dim("agent", t())
    reply_local = reply
    if lang and lang != "en-IN":
        t = _timer()
        reply_local = _to_lang(reply, lang)
        _dim("translate", t())
    console.print(f"[bold magenta]Vaani:[/] {reply_local}")
    if reply_local != reply:
        console.print(f"[dim]  (en) {reply}[/]")
    _speak(reply_local, lang, voice)


def lang_is_en(stt: dict) -> bool:
    return (stt.get("lang") or "en-IN").startswith("en")


def _setup_tools() -> None:
    try:
        from . import tools
        tools.set_confirm_callback(_confirm)
    except Exception as e:
        console.print(f"[yellow]Tools unavailable: {e}[/]")


def _ensure_key() -> bool:
    if not onboarding.current_key():
        console.print("[red]No Sarvam API key. Run `vaani setup` first.[/]")
        return False
    return True


# ---------- modes ----------
def run_text(text: str, lang: str | None, voice: bool) -> int:
    onboarding.apply_settings()
    if not _ensure_key():
        return 1
    _setup_tools()
    lang = lang or "en-IN"
    try:
        command_en = text
        if lang != "en-IN":
            from . import sarvam_client
            t = _timer()
            command_en = sarvam_client.translate(text, lang, "en-IN")
            _dim("translate", t())
            console.print(f"[dim]  (en) {command_en}[/]")
        _handle_command(command_en, lang, voice)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _voice_back_error(e, lang, voice)
        return 1


def run_loop(voice: bool) -> int:
    if onboarding.needs_setup():
        onboarding.run_setup()
    onboarding.apply_settings()
    if not _ensure_key():
        return 1
    _setup_tools()
    from . import trigger
    try:
        from .audio_io import ClipTooShort
    except Exception:
        class ClipTooShort(Exception):  # type: ignore
            pass

    ptt = config.PTT_KEY.upper()
    how = f"Say [bold]'Hey Vaani'[/] or hold [bold]{ptt}[/]" if config.WAKE_ENABLED else f"Hold [bold]{ptt}[/] and speak"
    console.print(Panel.fit(
        f"[bold cyan]Vaani {__version__}[/]  -  {how}\n"
        f"[dim]SAFE_MODE {'on' if config.SAFE_MODE else 'off'} | default language {config.DEFAULT_LANG} | Ctrl+C to quit[/]",
        border_style="cyan"))

    def on_listen(*_a):
        console.print("[green]Listening...[/]")

    try:
        while True:
            lang = config.DEFAULT_LANG
            try:
                console.print(f"\n[dim]Waiting ({'wake word / ' if config.WAKE_ENABLED else ''}{ptt})...[/]")
                res = trigger.wait_for_command(use_wake=config.WAKE_ENABLED, ptt_key=config.PTT_KEY,
                                               on_listen=on_listen)
                if isinstance(res, tuple):
                    wav, source = res
                else:
                    wav, source = res, "ptt"
                if not wav:
                    continue
                from . import sarvam_client
                t = _timer()
                stt = sarvam_client.stt_translate(wav)
                _dim("stt", t())
                if source == "wake":
                    from . import wakeword
                    raw0 = (stt.get("raw_transcript") or "").strip()
                    en0 = (stt.get("text_en") or "").strip()
                    matched, rest = wakeword.contains_wake(raw0)
                    rest_en = ""
                    m_en, r_en = wakeword.contains_wake(en0)
                    if m_en:
                        rest_en = (r_en or "").strip()
                    if not (matched or m_en):
                        console.print(f"[dim]  ignored: {_short(raw0 or en0, 100)}[/]")
                        continue
                    if not (rest or "").strip() and not rest_en:
                        # Wake word only: prompt and record the actual command.
                        from . import audio_io
                        audio_io.beep("listen")
                        on_listen()
                        wav = audio_io.record_until_silence()
                        t = _timer()
                        stt = sarvam_client.stt_translate(wav)
                        _dim("stt", t())
                    else:
                        # Wake word + command in one phrase: keep the English remainder.
                        if not rest_en and lang_is_en(stt):
                            rest_en = (rest or "").strip()
                        if not rest_en:
                            rest_en = en0
                        stt = dict(stt)
                        stt["text_en"] = rest_en
                        stt["raw_transcript"] = (rest or "").strip() or raw0
                command_en = (stt.get("text_en") or "").strip()
                lang = stt.get("lang") or config.DEFAULT_LANG
                if lang not in config.SUPPORTED_LANGS:
                    lang = config.DEFAULT_LANG
                raw = stt.get("raw_transcript") or ""
                if raw and raw != command_en:
                    console.print(f"[bold]You:[/] {raw}")
                console.print(f"[bold]You (en):[/] {command_en}  [dim][{lang}][/]")
                if not command_en:
                    continue
                _handle_command(command_en, lang, voice)
            except ClipTooShort:
                continue
            except KeyboardInterrupt:
                raise
            except Exception as e:
                _voice_back_error(e, lang, voice)
    except KeyboardInterrupt:
        console.print("\n[cyan]Bye![/]")
        return 0


def run_doctor() -> int:
    onboarding.apply_settings()
    table = Table(title="Vaani doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name, ok, detail=""):
        table.add_row(name, "[green]OK[/]" if ok else "[red]FAIL[/]", _short(detail, 80))

    key = onboarding.current_key()
    row("API key", bool(key), str(config.ENV_PATH) if key else "run `vaani setup`")
    if key:
        try:
            from . import sarvam_client
            ok, msg = sarvam_client.ping()
            row("Sarvam ping", ok, msg)
        except Exception as e:
            row("Sarvam ping", False, e)
    else:
        row("Sarvam ping", False, "skipped (no key)")
    try:
        from . import audio_io
        devs = audio_io.list_input_devices()
        row("Microphone", bool(devs), f"{len(devs)} input device(s)")
    except Exception as e:
        row("Microphone", False, e)
    row("Wake word", True,
        f"cloud-checked VAD (no local model), {'enabled' if config.WAKE_ENABLED else 'disabled'}")
    try:
        import pyautogui  # noqa: F401
        row("pyautogui import", True)
    except Exception as e:
        row("pyautogui import", False, e)
    row("Settings", config.SETTINGS_PATH.exists(),
        f"PTT={config.PTT_KEY} wake={config.WAKE_ENABLED} safe={config.SAFE_MODE} lang={config.DEFAULT_LANG}")
    console.print(table)
    if config.IS_WSL:
        console.print("[dim]WSL: audio may be unavailable; use `vaani --text`.[/]")
    return 0


# ---------- CLI ----------
def cli(argv=None) -> None:
    p = argparse.ArgumentParser(prog="vaani", description="Vaani - voice assistant for your laptop, powered by Sarvam AI.")
    p.add_argument("command", nargs="?", choices=["run", "setup", "doctor"], default="run",
                   help="run (default) | setup | doctor")
    p.add_argument("--text", "-t", help="typed command (skips the microphone)")
    p.add_argument("--lang", "-l", choices=config.SUPPORTED_LANGS,
                   help="language of --text and the reply (default en-IN)")
    p.add_argument("--no-voice", action="store_true", help="print the reply, don't speak it")
    p.add_argument("--version", action="version", version=f"vaani {__version__}")
    a = p.parse_args(argv)
    voice = not a.no_voice
    try:
        if a.command == "setup":
            onboarding.run_setup()
            code = 0
        elif a.command == "doctor":
            code = run_doctor()
        elif a.text:
            code = run_text(a.text, a.lang, voice)
        else:
            code = run_loop(voice)
    except KeyboardInterrupt:
        console.print("\n[cyan]Bye![/]")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    cli()
