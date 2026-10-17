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
    if s.get("mic_device") is not None:
        _use_mic(s["mic_device"])
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


def _use_mic(index) -> None:
    """Make the chosen input device the default for every recording."""
    try:
        import sounddevice as sd
        sd.default.device = (index, sd.default.device[1])
    except Exception:
        pass


def _probe(index, seconds: float = 0.5) -> float:
    """Peak input level (0-1) of one device, -1 if it can't be opened."""
    try:
        import numpy as np
        import sounddevice as sd
        x = sd.rec(int(seconds * config.SAMPLE_RATE), samplerate=config.SAMPLE_RATE, channels=1,
                   dtype="int16", device=index)
        sd.wait()
        return float(np.abs(x).max()) / 32768.0
    except Exception:
        return -1.0


def _step_mic(s: dict) -> None:
    console.print("\n[bold]Step 2 - Microphone[/]")
    try:
        from . import audio_io
        while True:
            devices = audio_io.list_input_devices()
            try:  # one entry per physical mic: keep only the default host API's devices
                import sounddevice as sd
                api = sd.default.hostapi
                devices = [d for d in devices if sd.query_devices(int(d.split(":", 1)[0]))["hostapi"] == api] or devices
            except Exception:
                pass
            if not devices:
                console.print("[yellow]No microphones found. You can still type commands in Vaani.[/]")
                return
            try:
                import sounddevice as sd
                default_in = sd.query_devices(kind="input")["name"]
            except Exception:
                default_in = "?"
            console.print(f"System default: [cyan]{default_in}[/]")
            console.print("Probing microphones [bold](make some noise / keep talking)[/]...")
            best, best_pk = None, 0.0
            for d in devices:
                idx = int(d.split(":", 1)[0])
                pk = _probe(idx)
                console.print(f"  {d}  [dim]{'level ' + format(pk, '.3f') if pk >= 0 else 'unavailable'}[/]")
                if pk > best_pk:
                    best, best_pk = idx, pk
            rec = s.get("mic_device") if s.get("mic_device") is not None else best
            if best is not None:
                console.print(f"Recommended: [green]{best}[/] (loudest signal)")
            choice = Prompt.ask("Mic number to use (Enter = recommended)", default="" if rec is None else str(rec))
            s["mic_device"] = int(choice) if choice.strip().isdigit() else None
            _use_mic(s["mic_device"])
            console.print("Mic test: [bold]speak now for 2 seconds...[/]")
            peak = audio_io.mic_check(2)
            if peak and peak > 0.02:
                console.print(f"[green]Mic OK[/] [dim](peak {peak:.2f})[/]")
                return
            console.print(f"[yellow]Very low input (peak {peak or 0:.3f}).[/] Wrong mic, muted, or no mic permission.")
            if not Confirm.ask("Pick a different mic / test again?", default=True):
                return
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
    key = (s.get("ptt_key") or "f9").upper()
    console.print(Panel.fit(
        "[bold]How will you talk to Vaani?[/]\n\n"
        "  [cyan]1[/]  Say [bold]'Hey Vaani'[/] hands-free, like Siri  [dim](+ hold " + key + " also works)[/]\n"
        "     -> needs a one-time voice recording NOW: you say 'Hey Vaani' 3 times (~15 s).\n"
        "        Matched locally on this laptop: no internet, no credits, no AI model.\n\n"
        "  [cyan]2[/]  Hold [bold]" + key + "[/] and speak only  [dim](no recording needed)[/]\n\n"
        "[dim]Without the recording, 'Hey Vaani' will NOT work - only " + key + ".[/]",
        title="Step 4 - Activation", border_style="magenta"))
    choice = Prompt.ask("Choose", choices=["1", "2"], default="1" if s.get("wake_enabled", True) else "2")
    if choice == "2":
        s["wake_enabled"] = False
        console.print(f"[green]OK - hold {key} to talk.[/] Enable the wake word later with [bold]vaani setup --wake[/].")
        return
    console.print("[bold]Recording your wake word.[/] After each beep, say [bold]'Hey Vaani'[/] and pause.")
    s["wake_enabled"] = _enroll_wake()
    if not s["wake_enabled"]:
        console.print(f"[yellow]Wake word not set up - hold {key} to talk.[/] Retry with [bold]vaani setup --wake[/].")


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

    console.print("\n[bold]Step 1 - Sarvam API key[/] [dim](stored only in " + str(config.ENV_PATH) + ")[/]")
    _step_key()
    _step_mic(s)
    console.print("\n[bold]Step 3 - Push-to-talk key[/]")
    s["ptt_key"] = Prompt.ask("Key to hold while speaking", default=s.get("ptt_key") or "f9").strip().lower()
    try:
        _step_wake(s)
    except Exception as e:
        console.print(f"[yellow]Wake word step failed: {e}. Use push-to-talk.[/]")
        s["wake_enabled"] = False
    console.print("\n[bold]Step 5 - Preferences[/]")
    try:
        s["safe_mode"] = Confirm.ask("SAFE_MODE: ask before pressing Enter/send/delete/closing windows?",
                                     default=bool(s.get("safe_mode", True)))
        s["default_lang"] = Prompt.ask("Default reply language (used when language detection is unsure)",
                                       choices=config.SUPPORTED_LANGS, default=s.get("default_lang") or "en-IN")
    except Exception as e:
        console.print(f"[yellow]Using default preferences ({e}).[/]")

    save_settings(s)
    apply_settings()
    key = s["ptt_key"].upper()
    how = (f"Say [bold]'Hey Vaani, <command>'[/] or hold [bold]{key}[/] and speak." if s.get("wake_enabled")
           else f"Hold [bold]{key}[/] and speak (wake word off - enable: [bold]vaani setup --wake[/]).")
    console.print(Panel.fit(f"[green]Setup complete.[/]\n{how}\nYou can also type commands. "
                            "Try: 'Chrome kholo aur YouTube pe lo-fi search karo'",
                            border_style="green"))
    return s
