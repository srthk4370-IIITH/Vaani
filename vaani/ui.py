"""Codex-CLI-style terminal UI: scrolling transcript + persistent prompt + live voice status.

Backends: prompt_toolkit (bottom toolbar, output printed above the prompt) or a rich-only
fallback (plain input() prompt) when prompt_toolkit isn't installed.
"""
from __future__ import annotations

import platform
import queue
import shutil
import sys
import threading
import time

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from . import __version__, config

try:  # optional dependency
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.patch_stdout import patch_stdout
    HAVE_PTK = True
except Exception:  # pragma: no cover
    HAVE_PTK = False

ACCENT = "cyan"
METER = " ▁▂▃▄▅▆▇"


def available() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def make_console() -> Console:
    # file=None -> rich writes to the *current* sys.stdout (patched by prompt_toolkit).
    width = shutil.get_terminal_size((100, 30)).columns
    return Console(force_terminal=True, highlight=False, width=width, soft_wrap=False)


# ---------------------------------------------------------------- rendering
def render_header(con: Console) -> None:
    os_name = f"{platform.system()} {platform.release()}"
    how = "Say “Hey Vaani …” · hold {k} · or type below" if config.WAKE_ENABLED else "hold {k} · or type below"
    body = Text()
    body.append("◆ Vaani", style=f"bold {ACCENT}")
    body.append(f"  v{__version__}\n\n", style="dim")
    body.append("model   ", style="dim"); body.append("saaras:v4 → sarvam-105b → bulbul:v4\n")
    body.append("os      ", style="dim"); body.append(os_name + "\n")
    body.append("safe    ", style="dim")
    body.append("on" if config.SAFE_MODE else "off", style="green" if config.SAFE_MODE else "yellow")
    body.append(f"   lang {config.DEFAULT_LANG}\n", style="dim")
    body.append("\n" + how.format(k=config.PTT_KEY.upper()), style="dim")
    con.print(Panel(body, border_style="grey37", expand=False, padding=(0, 2)))
    con.print("[dim]  /help for commands · Ctrl+C to quit[/]\n")


def render_user(con: Console, text_en: str, lang: str = "", raw: str = "") -> None:
    line = Text("› ", style=f"bold {ACCENT}")
    line.append(text_en or raw, style="bold")
    if lang:
        line.append(f"  [{lang}]", style="dim")
    con.print(line)
    if raw and raw.strip() != (text_en or "").strip():
        con.print(Text("  " + raw, style="dim"))


def _fmt_args(args) -> str:
    if isinstance(args, dict):
        parts = []
        for k, v in args.items():
            v = repr(v)
            parts.append(f"{k}={v if len(v) <= 40 else v[:37] + '...'}")
        return ", ".join(parts)
    return str(args)[:60]


def render_tool(con: Console, name: str, args, ok: bool, result) -> None:
    res = str(result).replace("\n", " ")
    if len(res) > 90:
        res = res[:87] + "..."
    t = Text("  ⎿ ", style="dim")
    t.append(f"{name}({_fmt_args(args)})", style=ACCENT)
    t.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "red")
    t.append(res, style="dim" if ok else "red")
    con.print(t)


def render_reply(con: Console, reply_local: str, reply_en: str = "") -> None:
    t = Text("◆ Vaani  ", style=f"bold {ACCENT}")
    t.append(reply_local)
    con.print(t)
    if reply_en and reply_en.strip() != reply_local.strip():
        con.print(Text("  " + reply_en, style="dim"))


def render_footer(con: Console, timings: dict) -> None:
    parts = [f"{k} {timings[k]:.1f}s" for k in ("stt", "agent", "translate", "tts") if timings.get(k)]
    if parts:
        con.print(Text("  " + " · ".join(parts), style="dim"))
    con.print()


def render_error(con: Console, msg: str) -> None:
    con.print(Text("  ✗ " + msg, style="red"))


HELP = """[bold]Commands[/]
  /help             this help
  /quit             exit (also Ctrl+C / Ctrl+D)
  /clear            clear the screen
  /safe on|off      toggle SAFE_MODE confirmations
  /lang hi-IN       reply language for typed commands (or 'auto')
  /voice on|off     speak replies / mute TTS
Anything else is sent to Vaani. Voice: say “Hey Vaani …” or hold the push-to-talk key."""


# ---------------------------------------------------------------- app
class VaaniUI:
    def __init__(self, voice: bool):
        self.con = make_console()
        self.voice = voice
        self.lang_override: str | None = None
        self.status = "idle"
        self.level = 0.0
        self.cmd_q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.busy = threading.Event()
        self.pending: dict | None = None
        self.app = None
        self.voice_ok = True

    # --- state
    def set_status(self, s: str) -> None:
        self.status = s
        if self.app is not None:
            try:
                self.app.invalidate()
            except Exception:
                pass
        elif s not in ("idle", "listening"):
            self.con.print(Text(f"  {self._status_text()}", style="dim"))

    def _status_text(self) -> str:
        if self.pending:
            return f"⚠ confirm: {self.pending['q']} [y/N]"
        s = self.status
        if s == "listening":
            n = min(len(METER) - 1, int(self.level / 400))
            bars = "".join(METER[min(len(METER) - 1, max(0, n - i))] for i in range(5))
            return f"◉ listening… {bars}"
        if s == "transcribing":
            return "◌ transcribing…"
        if s == "thinking":
            return "◌ thinking…"
        if s == "speaking":
            return "▶ speaking…"
        if not self.voice_ok:
            return "● idle — voice unavailable, type below"
        how = "say Hey Vaani / hold " if config.WAKE_ENABLED else "hold "
        return f"● idle — {how}{config.PTT_KEY.upper()}"

    def toolbar(self):
        s = self._status_text()
        extra = f"   safe {'on' if config.SAFE_MODE else 'off'} · voice {'on' if self.voice else 'off'}"
        if self.pending:
            return HTML(f"<style fg='ansiyellow'>{_html(s)}</style>")
        return HTML(f"{_html(s)}<style fg='ansibrightblack'>{_html(extra)}</style>")

    # --- confirm (called from worker thread by tools/guardrails)
    def confirm(self, question: str) -> bool:
        ev = threading.Event()
        self.pending = {"q": question, "ev": ev, "ans": False}
        self.con.print(Text(f"  ⚠ Vaani wants to: {question}  [y/N]", style="yellow"))
        self.set_status(self.status)
        ev.wait(30)
        ans = bool(self.pending and self.pending.get("ans"))
        if not ev.is_set():
            self.con.print(Text("  (no answer in 30s — declined)", style="dim"))
        self.pending = None
        self.set_status(self.status)
        return ans

    # --- mic level meter (wrap audio_io._rms, used by the VAD loop)
    def _hook_level(self) -> None:
        try:
            from . import audio_io
            orig = audio_io._rms

            def rms(block):
                v = orig(block)
                self.level = v
                if self.status == "listening" and self.app is not None:
                    try:
                        self.app.invalidate()
                    except Exception:
                        pass
                return v
            audio_io._rms = rms
        except Exception:
            pass

    # --- threads
    def voice_loop(self) -> None:
        from . import audio_io, trigger
        while not self.stop.is_set():
            while self.busy.is_set() and not self.stop.is_set():
                time.sleep(0.1)
            try:
                res = trigger.wait_for_command(use_wake=config.WAKE_ENABLED, ptt_key=config.PTT_KEY,
                                               on_listen=lambda *_: self.set_status("listening"))
                wav, source = res if isinstance(res, tuple) else (res, "ptt")
                if wav and not self.stop.is_set():
                    self.busy.set()
                    self.cmd_q.put(("voice", wav, source))
                else:
                    self.set_status("idle")
            except audio_io.ClipTooShort:
                self.set_status("idle")
            except audio_io.AudioUnavailable as e:
                self.voice_ok = False
                self.con.print(Text(f"  voice unavailable: {e} — typing still works", style="yellow"))
                self.set_status("idle")
                return
            except Exception as e:
                render_error(self.con, f"mic: {e}")
                self.set_status("idle")
                time.sleep(1.0)

    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                item = self.cmd_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self.busy.set()
            try:
                if item[0] == "voice":
                    self._process_voice(item[1], item[2])
                else:
                    self._process_text(item[1])
            except Exception as e:
                self._error(e, getattr(self, "_cur_lang", config.DEFAULT_LANG))
            finally:
                self.busy.clear()
                self.set_status("idle")

    # --- pipeline
    def _process_voice(self, wav: str, source: str) -> None:
        from . import main, sarvam_client
        timings: dict = {"on_stt": lambda: self.set_status("transcribing")}
        self.set_status("transcribing")
        t0 = time.perf_counter()
        stt = sarvam_client.stt_translate(wav)
        timings["stt"] = time.perf_counter() - t0
        stt = main.resolve_wake(stt, source, lambda *_: self.set_status("listening"), timings)
        if stt is None:
            return
        command_en = (stt.get("text_en") or "").strip()
        lang = stt.get("lang") or config.DEFAULT_LANG
        if lang not in config.SUPPORTED_LANGS:
            lang = config.DEFAULT_LANG
        if not command_en:
            return
        render_user(self.con, command_en, lang, stt.get("raw_transcript") or "")
        self._run(command_en, lang, timings)

    def _process_text(self, line: str) -> None:
        if self.lang_override:
            lang = self.lang_override
        else:
            lang = "en-IN" if line.isascii() else config.DEFAULT_LANG
        render_user(self.con, line, lang if lang != "en-IN" else "")
        self._run(line, lang, {})

    def _run(self, command_en: str, lang: str, timings: dict) -> None:
        from . import agent, main
        self._cur_lang = lang
        self.set_status("thinking")
        last: dict = {}

        def on_event(kind, data):
            try:
                if kind == "tool_call":
                    last.clear(); last.update(data)
                elif kind == "tool_result":
                    render_tool(self.con, data.get("name") or last.get("name", "?"),
                                data.get("args", last.get("args", {})), bool(data.get("ok")),
                                data.get("result", ""))
            except Exception:
                pass

        t0 = time.perf_counter()
        reply = agent.run_agent(command_en, lang, on_event)
        timings["agent"] = time.perf_counter() - t0
        reply_local = reply
        if lang and lang != "en-IN":
            t0 = time.perf_counter()
            try:
                reply_local = main._to_lang(reply, lang)
            except Exception:
                reply_local = reply
            timings["translate"] = time.perf_counter() - t0
        render_reply(self.con, reply_local, reply)
        self._speak(reply_local, lang, timings)
        render_footer(self.con, timings)

    def _speak(self, text: str, lang: str, timings: dict | None = None) -> None:
        if not self.voice:
            return
        from . import audio_io, sarvam_client
        self.set_status("speaking")
        t0 = time.perf_counter()
        audio = sarvam_client.tts(text, lang or "en-IN")
        if timings is not None:
            timings["tts"] = time.perf_counter() - t0
        audio_io.play_audio(audio)

    def _error(self, e: Exception, lang: str) -> None:
        from . import main
        render_error(self.con, main._short(e, 300))
        msg = main._friendly_error(e)
        try:
            local = main._to_lang(msg, lang)
            render_reply(self.con, local, msg)
            self._speak(local, lang)
        except Exception:
            try:
                from . import audio_io
                audio_io.beep("error")
            except Exception:
                pass
        self.con.print()

    # --- input handling (main thread)
    def handle_line(self, line: str) -> bool:
        """Returns False to quit."""
        line = line.strip()
        if self.pending:
            self.pending["ans"] = line.lower() in ("y", "yes", "haan", "ha", "han", "ok")
            self.pending["ev"].set()
            return True
        if not line:
            return True
        if line.startswith("/"):
            return self._slash(line)
        self.cmd_q.put(("text", line))
        return True

    def _slash(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            self.con.print(HELP)
        elif cmd == "/clear":
            self.con.clear()
            render_header(self.con)
        elif cmd == "/safe" and arg.lower() in ("on", "off"):
            config.SAFE_MODE = arg.lower() == "on"
            self.con.print(f"[dim]  SAFE_MODE {arg.lower()}[/]")
        elif cmd == "/voice" and arg.lower() in ("on", "off"):
            self.voice = arg.lower() == "on"
            self.con.print(f"[dim]  voice replies {arg.lower()}[/]")
        elif cmd == "/lang":
            if arg in config.SUPPORTED_LANGS:
                self.lang_override = arg
                self.con.print(f"[dim]  typed replies in {arg}[/]")
            elif arg.lower() == "auto":
                self.lang_override = None
                self.con.print("[dim]  typed replies: auto[/]")
            else:
                self.con.print(f"[dim]  languages: {', '.join(config.SUPPORTED_LANGS)}[/]")
        else:
            self.con.print(f"[dim]  unknown command {escape(line)} — /help[/]")
        self.set_status(self.status)
        return True

    # --- run
    def start_threads(self) -> None:
        if getattr(self, "_started", False):
            return
        self._started = True
        self._hook_level()
        try:
            from . import tools
            tools.set_confirm_callback(self.confirm)
        except Exception:
            pass
        threading.Thread(target=self.worker, daemon=True, name="vaani-worker").start()
        threading.Thread(target=self.voice_loop, daemon=True, name="vaani-voice").start()

    def run_ptk(self) -> int:
        session = PromptSession(bottom_toolbar=self.toolbar, refresh_interval=0.1)
        self.app = session.app
        with patch_stdout(raw=True):
            render_header(self.con)
            self.start_threads()
            while True:
                try:
                    line = session.prompt([("class:prompt", "› ")])
                except (KeyboardInterrupt, EOFError):
                    break
                if not self.handle_line(line):
                    break
        return self._quit()

    def run_basic(self) -> int:
        render_header(self.con)
        self.start_threads()
        while True:
            try:
                line = self.con.input(f"[bold {ACCENT}]› [/]")
            except (KeyboardInterrupt, EOFError):
                break
            if not self.handle_line(line):
                break
        return self._quit()

    def _quit(self) -> int:
        self.stop.set()
        if self.pending:
            self.pending["ev"].set()
        self.app = None
        self.con.print(f"[{ACCENT}]Bye![/]")
        return 0


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_ui(voice: bool) -> int:
    ui = VaaniUI(voice)
    if HAVE_PTK:
        try:
            return ui.run_ptk()
        except Exception as e:  # e.g. unsupported console
            ui.app = None
            ui.con.print(f"[dim]prompt_toolkit unavailable ({e}); using basic prompt[/]")
    return ui.run_basic()
