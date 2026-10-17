"""`vaani setup`: local onboarding. Only the optional key ping talks to Sarvam."""
import json
import os
from getpass import getpass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from . import config

console = Console()

DEFAULT_SETTINGS = {
    "ptt_key": "f9",
    "wake_enabled": True,
    "safe_mode": True,
    "default_lang": "en-IN",
    "input_device": None,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        if config.SETTINGS_PATH.exists():
            s.update(json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    config.SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _read_env_key() -> str:
    try:
        for line in config.ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SARVAM_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def current_key() -> str:
    return config.SARVAM_API_KEY or os.environ.get("SARVAM_API_KEY", "") or _read_env_key()


def save_key(key: str) -> None:
    lines = []
    try:
        lines = [l for l in config.ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("SARVAM_API_KEY=")]
    except Exception:
        pass
    lines.append(f"SARVAM_API_KEY={key}")
    config.ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(config.ENV_PATH, 0o600)
    except Exception:
        pass
    os.environ["SARVAM_API_KEY"] = key
    config.SARVAM_API_KEY = key


def needs_setup() -> bool:
    return not current_key() or not config.SETTINGS_PATH.exists()


def apply_settings() -> dict:
    s = load_settings()
    config.PTT_KEY = s.get("ptt_key") or config.PTT_KEY
    config.WAKE_ENABLED = bool(s.get("wake_enabled", config.WAKE_ENABLED))
    config.SAFE_MODE = bool(s.get("safe_mode", config.SAFE_MODE))
    lang = s.get("default_lang")
    if lang in config.SUPPORTED_LANGS:
        config.DEFAULT_LANG = lang
    key = current_key()
    if key:
        os.environ["SARVAM_API_KEY"] = key
        config.SARVAM_API_KEY = key
    return s


def _step_key() -> None:
    existing = current_key()
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 8 else "****"
        if not Confirm.ask(f"An API key is already saved ({masked}). Replace it?", default=False):
            return
    while True:
        key = getpass("Paste your Sarvam API key (input hidden): ").strip()
        if key:
            break
        console.print("[yellow]Key cannot be empty. Get one at https://dashboard.sarvam.ai[/]")
    save_key(key)
    console.print(f"[green]Saved to {config.ENV_PATH}[/]")
    try:
        from . import sarvam_client
        with console.status("Checking key with Sarvam..."):
            ok, msg = sarvam_client.ping()
        if ok:
            console.print(f"[green]Key works.[/] {msg}")
        else:
            console.print(f"[yellow]Could not verify key: {msg}. Saved anyway; run `vaani doctor` later.[/]")
    except Exception as e:
        console.print(f"[yellow]Could not verify key ({e}). Saved anyway.[/]")


def _step_mic(s: dict) -> None:
    try:
        from . import audio_io
        devices = audio_io.list_input_devices()
        if not devices:
            console.print("[yellow]No microphones found.[/]")
        else:
            console.print("[bold]Microphones:[/]")
            for d in devices:
                console.print(f"  {d}")
        if Confirm.ask("Run a 2-second mic test now?", default=True):
            console.print("Speak now...")
            peak = audio_io.mic_check(2)
            if peak and peak > 0.02:
                console.print(f"[green]Mic OK (peak {peak:.2f}).[/]")
            else:
                console.print(f"[yellow]Very low input (peak {peak or 0:.3f}). Check mic permissions/volume.[/]")
    except Exception as e:
        console.print(f"[yellow]Mic check skipped: {e}[/]")
        if config.IS_WSL:
            console.print("[dim]Tip: WSL often has no audio. Use `vaani --text \"...\"` or run on Windows directly.[/]")


def _enroll_wake() -> bool:
    """Record 3 samples, then a verify test. Returns True when enrolled."""
    from . import wakeword
    while True:
        console.print("[dim]Speak naturally, about as loud as you normally would.[/]")
        try:
            data = wakeword.enroll(3, on_prompt=lambda m: console.print(f"  [cyan]{m}[/]"))
        except Exception as e:
            console.print(f"[yellow]Enrollment failed: {e}[/]")
            if Confirm.ask("Try again?", default=True):
                continue
            return False
        console.print(f"[green]Saved 3 samples[/] [dim](threshold {data['threshold']:.1f})[/]")
        console.print("  [cyan]Verify: say 'Hey Vaani' once more...[/]")
        try:
            from . import audio_io
            wav = audio_io.record_until_silence(max_sec=4.0, silence_sec=0.6, no_speech_timeout=6.0)
            ok, score, _ = wakeword.matches(wav)
            console.print(f"  {'[green]matched[/]' if ok else '[red]not matched[/]'} "
                          f"[dim](score {score:.1f} / threshold {data['threshold']:.1f}, lower is closer)[/]")
        except Exception as e:
            ok = False
            console.print(f"  [yellow]verify skipped: {e}[/]")
        if not Confirm.ask("Re-record the wake word?", default=not ok):
            return True


def _step_wake(s: dict) -> None:
    console.print(
        "[bold]Wake word 'Hey Vaani'[/]: detected [bold]locally[/] on your laptop with classic signal processing "
        "(MFCC + DTW) against 3 samples of your voice. [dim]No internet, no API credits, no AI model; "
        "only the command you say after it is sent to Sarvam.[/]\n"
        f"Push-to-talk (hold {(s.get('ptt_key') or 'f9').upper()}) always works too.")
    s["wake_enabled"] = Confirm.ask("Enable the 'Hey Vaani' wake word (record 3 samples now)?",
                                    default=bool(s.get("wake_enabled", True)))
    if s["wake_enabled"]:
        s["wake_enabled"] = _enroll_wake()


def run_wake_setup() -> None:
    """`vaani setup --wake`: (re-)enroll the wake word only."""
    s = load_settings()
    _step_wake(s)
    save_settings(s)
    apply_settings()


def run_setup() -> dict:
    console.print(Panel.fit(
        "[bold]Welcome to Vaani[/] - a voice assistant for your laptop, powered by Sarvam AI.\n\n"
        "Runs locally: settings, your API key, voice detection, recording, playback and laptop control.\n"
        "No local AI models. 'Hey Vaani' is matched locally (no credits). AI calls (speech-to-text of\n"
        "your command, translation, text-to-speech,\n"
        "the agent, screen reading) go to Sarvam's cloud.",
        title="Vaani setup", border_style="cyan"))
    if config.IS_WSL:
        console.print("[dim]WSL detected: audio devices may be missing. Typed mode (`vaani --text`) always works.[/]")
    s = load_settings()

    _step_key()
    _step_mic(s)
    try:
        _step_wake(s)
    except Exception as e:
        console.print(f"[yellow]Wake word step failed: {e}[/]")
        s["wake_enabled"] = False

    try:
        s["ptt_key"] = Prompt.ask("Push-to-talk key (hold to speak)", default=s.get("ptt_key") or "f9").strip().lower()
        s["safe_mode"] = Confirm.ask("SAFE_MODE: ask before pressing Enter/send/delete?", default=bool(s.get("safe_mode", True)))
        s["default_lang"] = Prompt.ask("Default reply language", choices=config.SUPPORTED_LANGS,
                                       default=s.get("default_lang") or "en-IN")
    except Exception as e:
        console.print(f"[yellow]Using default preferences ({e}).[/]")

    save_settings(s)
    apply_settings()
    wake = "Say 'Hey Vaani' or " if s.get("wake_enabled") else ""
    console.print(Panel.fit(f"Setup complete. Run [bold]vaani[/], then {wake}hold [bold]{s['ptt_key'].upper()}[/] and speak.",
                            border_style="green"))
    return s
