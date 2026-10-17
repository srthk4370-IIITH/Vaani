"""Laptop-control tools exposed to the Vaani agent.

Contract:
    TOOL_SCHEMAS: list[dict]            OpenAI-style function schemas
    execute_tool(name, args, user_command="") -> dict  {"ok", "result"}; never raises; guardrails.check first
    set_confirm_callback(fn)            fn(question: str) -> bool, used by SAFE_MODE gate
"""
from __future__ import annotations

import datetime
import difflib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import config

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WSL = bool(getattr(config, "IS_WSL", False))

APP_NAME_RE = re.compile(r"^[A-Za-z0-9 ._+\-]{1,60}$")
RISKY_RE = re.compile(
    r"\b(send|delete|del|remove|purchase|pay|payment|buy|order|checkout|format|rm|shutdown|transfer|submit)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- confirm gate

def _console_confirm(question: str) -> bool:
    try:
        ans = input(f"[Vaani] {question} (y/n): ")
    except Exception:
        return False
    return ans.strip().lower() in ("y", "yes", "haan", "ha", "han")


_confirm_cb = _console_confirm


def set_confirm_callback(fn):
    global _confirm_cb
    _confirm_cb = fn if callable(fn) else _console_confirm


_preconfirmed = False  # set while executing an action the user already approved via guardrails


def _confirm(question: str) -> bool:
    if _preconfirmed or not config.SAFE_MODE:
        return True
    try:
        return bool(_confirm_cb(question))
    except Exception:
        return False


def _ok(msg) -> dict:
    return {"ok": True, "result": str(msg)}


def _fail(msg) -> dict:
    return {"ok": False, "result": str(msg)}


DECLINED = _fail("User declined")

# --------------------------------------------------------------------------- helpers

def _gui():
    """Lazy pyautogui import. Returns (module, None) or (None, reason)."""
    try:
        import pyautogui  # noqa
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui, None
    except Exception as e:  # headless / WSL / missing display
        return None, f"GUI automation unavailable: {type(e).__name__}: {e}"


MOD = "command" if IS_MAC else "ctrl"


def _popen(argv):
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def _run(argv, timeout=8):
    kw = {}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)


def _open_target(target: str) -> None:
    """Open a URL / file with the OS default handler (no shell interpolation)."""
    if IS_WIN:
        os.startfile(target)  # type: ignore[attr-defined]
    elif IS_WSL:
        if shutil.which("wslview"):
            _popen(["wslview", target])
        else:
            _popen(["cmd.exe", "/c", "start", "", target.replace("&", "^&")])
    elif IS_MAC:
        _popen(["open", target])
    else:
        _popen(["xdg-open", target])

# --------------------------------------------------------------------------- open_app

WIN_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome", "browser": "msedge", "edge": "msedge",
    "microsoft edge": "msedge", "firefox": "firefox", "notepad": "notepad",
    "calculator": "calc", "calc": "calc", "vscode": "code", "vs code": "code", "code": "code",
    "visual studio code": "code", "terminal": "wt", "cmd": "cmd", "command prompt": "cmd",
    "powershell": "powershell", "file explorer": "explorer", "explorer": "explorer",
    "files": "explorer", "spotify": "spotify:", "settings": "ms-settings:",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
    "paint": "mspaint", "task manager": "taskmgr", "whatsapp": "whatsapp:",
    "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
    "snipping tool": "snippingtool", "control panel": "control",
}
MAC_ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "browser": "Safari",
    "safari": "Safari", "firefox": "Firefox", "notepad": "TextEdit", "textedit": "TextEdit",
    "calculator": "Calculator", "calc": "Calculator", "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code", "code": "Visual Studio Code", "terminal": "Terminal",
    "file explorer": "Finder", "explorer": "Finder", "finder": "Finder", "files": "Finder",
    "spotify": "Spotify", "settings": "System Settings", "word": "Microsoft Word",
    "excel": "Microsoft Excel", "powerpoint": "Microsoft PowerPoint", "whatsapp": "WhatsApp",
    "notes": "Notes", "mail": "Mail",
}
LINUX_ALIASES = {
    "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "browser": ["xdg-open:https://www.google.com", "firefox", "google-chrome", "chromium"],
    "firefox": ["firefox"], "notepad": ["gedit", "gnome-text-editor", "kate", "mousepad", "xed"],
    "calculator": ["gnome-calculator", "kcalc", "galculator"], "calc": ["gnome-calculator", "kcalc"],
    "vscode": ["code"], "vs code": ["code"], "code": ["code"],
    "terminal": ["x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"],
    "file explorer": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "files": ["nautilus", "dolphin", "thunar", "nemo"], "spotify": ["spotify"],
    "settings": ["gnome-control-center", "systemsettings"], "word": ["libreoffice --writer"],
    "excel": ["libreoffice --calc"],
}


_LNK_CACHE = None


def _win_lnk_index():
    global _LNK_CACHE
    if _LNK_CACHE is not None:
        return _LNK_CACHE
    roots = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            roots.append((Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs", True))
    roots.append((Path.home() / "Desktop", False))
    pub = os.environ.get("PUBLIC")
    if pub:
        roots.append((Path(pub) / "Desktop", False))
    cands = {}
    for r, recursive in roots:
        if not r.exists():
            continue
        try:
            for p in (r.rglob("*") if recursive else r.glob("*")):
                if p.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                    stem = p.stem.lower()
                    if "uninstall" in stem:
                        continue
                    cands.setdefault(stem, p)
        except Exception:
            continue
    _LNK_CACHE = cands
    return cands


def _win_start_menu_find(name: str):
    cands = _win_lnk_index()
    q = name.lower().strip()
    if not cands:
        return None
    if q in cands:
        return cands[q]
    # prefix / substring match, prefer shortest
    subs = sorted([s for s in cands if s.startswith(q) or q in s], key=len)
    if subs:
        return cands[subs[0]]
    close = difflib.get_close_matches(q, list(cands), n=1, cutoff=0.75)
    return cands[close[0]] if close else None


def _win_appx_find(name: str):
    """Search installed Store/UWP apps via Get-StartApps (name is regex-validated)."""
    try:
        ps = ("Get-StartApps | Where-Object { $_.Name -like '*" + name.replace("'", "") +
              "*' } | Select-Object -First 1 -ExpandProperty AppID")
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=8)
        appid = (r.stdout or "").strip().splitlines()
        return appid[0].strip() if appid and appid[0].strip() else None
    except Exception:
        return None


def _win_app_path(exe: str):
    """Look up HKLM/HKCU ...\\App Paths\\<exe>.exe so `start` won't pop an error dialog."""
    try:
        import winreg
    except ImportError:
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + exe + ".exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, _ = winreg.QueryValueEx(k, "")
                if val:
                    return val
        except OSError:
            continue
    return None


def _open_app_windows(name: str):
    key = name.lower().strip()
    alias = WIN_ALIASES.get(key)
    if alias:
        if alias.endswith(":"):  # URI protocol
            try:
                os.startfile(alias)  # type: ignore[attr-defined]
                return True, f"Opened {name}"
            except Exception:
                pass
        else:
            exe = shutil.which(alias)
            if exe:
                _popen([exe])
                return True, f"Opened {name}"
            # App Paths registered (chrome, winword, excel...) -> launch via `start`
            if APP_NAME_RE.match(alias) and _win_app_path(alias):
                try:
                    _popen(["cmd", "/c", "start", "", alias])
                    return True, f"Opened {name}"
                except Exception:
                    pass
    exe = shutil.which(key) or shutil.which(key.replace(" ", ""))
    if exe:
        _popen([exe])
        return True, f"Opened {name}"
    lnk = _win_start_menu_find(name)
    if lnk:
        os.startfile(str(lnk))  # type: ignore[attr-defined]
        return True, f"Opened {lnk.stem}"
    appid = _win_appx_find(name)
    if appid:
        _popen(["explorer.exe", f"shell:AppsFolder\\{appid}"])
        return True, f"Opened {name}"
    return False, None


def _open_app_wsl(name: str):
    key = name.lower().strip()
    alias = WIN_ALIASES.get(key, key)
    exe = shutil.which(key)
    if exe and os.environ.get("DISPLAY"):
        _popen([exe])
        return True, f"Opened {name}"
    if APP_NAME_RE.match(alias):
        try:
            r = _run(["cmd.exe", "/c", "start", "", alias], timeout=6)
            if r.returncode == 0 and not r.stderr.strip():
                return True, f"Opened {name}"
        except Exception:
            pass
    return False, None


def _open_app_mac(name: str):
    app = MAC_ALIASES.get(name.lower().strip(), name)
    try:
        r = _run(["open", "-a", app], timeout=8)
        if r.returncode == 0:
            return True, f"Opened {app}"
    except Exception:
        pass
    return False, None


def _open_app_linux(name: str):
    key = name.lower().strip()
    for cand in LINUX_ALIASES.get(key, []) + [key, key.replace(" ", "-"), key.replace(" ", "")]:
        if cand.startswith("xdg-open:"):
            if shutil.which("xdg-open"):
                _popen(["xdg-open", cand.split(":", 1)[1]])
                return True, f"Opened {name}"
            continue
        parts = cand.split()
        if shutil.which(parts[0]):
            _popen(parts)
            return True, f"Opened {name}"
    # .desktop search
    dirs = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications")]
    best = None
    for d in dirs:
        if not d.exists():
            continue
        for f in d.glob("*.desktop"):
            try:
                txt = f.read_text(errors="ignore")
            except Exception:
                continue
            m = re.search(r"^Name=(.+)$", txt, re.M)
            nm = (m.group(1) if m else f.stem).lower()
            if key == nm or key in nm or key in f.stem.lower():
                best = f
                break
        if best:
            break
    if best and shutil.which("gtk-launch"):
        _popen(["gtk-launch", best.stem])
        return True, f"Opened {name}"
    return False, None


def open_app(name: str = "") -> dict:
    name = (name or "").strip()
    if not APP_NAME_RE.match(name):
        return _fail(f"Invalid app name '{name[:60]}'")
    if IS_WIN:
        ok, msg = _open_app_windows(name)
    elif IS_WSL:
        ok, msg = _open_app_wsl(name)
    elif IS_MAC:
        ok, msg = _open_app_mac(name)
    else:
        ok, msg = _open_app_linux(name)
    if not ok:
        return _fail(f"App '{name}' not found on this computer")
    time.sleep(1.5)
    return _ok(msg)

# --------------------------------------------------------------------------- web

def open_url(url: str = "") -> dict:
    url = (url or "").strip()
    if not url:
        return _fail("No URL given")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc or any(c in url for c in '\r\n"<>` '):
        return _fail(f"Refusing to open unsafe URL '{url[:100]}'")
    _open_target(url)
    time.sleep(1.5)
    return _ok(f"Opened {url}")


def search_web(query: str = "", site: str = "google") -> dict:
    query = (query or "").strip()
    if not query:
        return _fail("No query given")
    q = urllib.parse.quote_plus(query)
    if (site or "").lower().startswith("you"):
        url = f"https://www.youtube.com/results?search_query={q}"
    else:
        url = f"https://www.google.com/search?q={q}"
    r = open_url(url)
    if r["ok"]:
        r["result"] = f"Searched {'YouTube' if 'youtube' in url else 'Google'} for '{query}'"
    return r

# --------------------------------------------------------------------------- keyboard / mouse

def _risky(s: str) -> bool:
    return bool(RISKY_RE.search(s or ""))


def type_text(text: str = "", press_enter: bool = False) -> dict:
    text = str(text or "")
    if not text and not press_enter:
        return _fail("No text given")
    if press_enter or _risky(text):
        q = f"Type '{text[:80]}'" + (" and press Enter" if press_enter else "") + "?"
        if not _confirm(q):
            return DECLINED
    gui, err = _gui()
    if err:
        return _fail(err)
    try:
        if text:
            if text.isascii():
                gui.write(text, interval=0.01)
            else:
                try:
                    import pyperclip
                except ImportError:
                    return _fail("pyperclip not installed; cannot type non-ASCII text")
                old = None
                try:
                    old = pyperclip.paste()
                except Exception:
                    pass
                pyperclip.copy(text)
                time.sleep(0.05)
                gui.hotkey(MOD, "v")
                time.sleep(0.15)
                if old is not None:
                    try:
                        pyperclip.copy(old)
                    except Exception:
                        pass
        if press_enter:
            gui.press("enter")
        return _ok(f"Typed {len(text)} characters" + (" and pressed Enter" if press_enter else ""))
    except Exception as e:
        return _fail(f"Typing failed: {e}")


KEY_ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "control": "ctrl",
               "cmd": "command", "win": "winleft", "windows": "winleft", "option": "alt",
               "spacebar": "space", "pgup": "pageup", "pgdn": "pagedown"}


def _norm_key(k: str) -> str:
    k = str(k or "").strip().lower()
    k = KEY_ALIASES.get(k, k)
    if IS_MAC and k == "ctrl":
        pass  # caller explicitly asked for ctrl
    return k


def press_key(key: str = "") -> dict:
    k = _norm_key(key)
    if not k:
        return _fail("No key given")
    gui, err = _gui()
    if err:
        return _fail(err)
    if k not in gui.KEYBOARD_KEYS:
        return _fail(f"Unknown key '{key}'")
    if k in ("enter", "delete") and not _confirm(f"Press '{k}'?"):
        return DECLINED
    try:
        gui.press(k)
        return _ok(f"Pressed {k}")
    except Exception as e:
        return _fail(f"Key press failed: {e}")


def hotkey(keys=None) -> dict:
    if isinstance(keys, str):
        keys = re.split(r"[+\s,]+", keys)
    keys = [_norm_key(k) for k in (keys or []) if str(k).strip()]
    if not keys:
        return _fail("No keys given")
    gui, err = _gui()
    if err:
        return _fail(err)
    bad = [k for k in keys if k not in gui.KEYBOARD_KEYS]
    if bad:
        return _fail(f"Unknown key(s): {', '.join(bad)}")
    combo = "+".join(keys)
    dangerous = {"alt+f4", "ctrl+w", "command+w", "command+q", "ctrl+q", "shift+delete",
                 "ctrl+shift+delete", "command+delete", "ctrl+enter", "command+enter"}
    if (combo in dangerous or "delete" in keys or "enter" in keys) and not _confirm(f"Press {combo}?"):
        return DECLINED
    try:
        gui.hotkey(*keys)
        return _ok(f"Pressed {combo}")
    except Exception as e:
        return _fail(f"Hotkey failed: {e}")


def scroll(amount=0) -> dict:
    try:
        amt = int(float(amount))
    except Exception:
        return _fail("amount must be a number")
    amt = max(-50, min(50, amt))
    gui, err = _gui()
    if err:
        return _fail(err)
    try:
        gui.scroll(amt * 100 if not IS_MAC else amt * 5)
        return _ok(f"Scrolled {'up' if amt > 0 else 'down'} {abs(amt)}")
    except Exception as e:
        return _fail(f"Scroll failed: {e}")


def close_window() -> dict:
    if not _confirm("Close the current window?"):
        return DECLINED
    gui, err = _gui()
    if err:
        return _fail(err)
    try:
        if IS_MAC:
            gui.hotkey("command", "w")
        else:
            gui.hotkey("alt", "f4")
        return _ok("Closed the active window")
    except Exception as e:
        return _fail(f"Close failed: {e}")

# --------------------------------------------------------------------------- volume

def set_volume(level=50) -> dict:
    try:
        lv = int(float(level))
    except Exception:
        return _fail("level must be a number 0-100")
    lv = max(0, min(100, lv))
    if IS_WIN:
        try:
            from pycaw.pycaw import AudioUtilities  # type: ignore
            dev = AudioUtilities.GetSpeakers()
            vol = getattr(dev, "EndpointVolume", None)
            if vol is None:
                from ctypes import POINTER, cast
                from comtypes import CLSCTX_ALL  # type: ignore
                from pycaw.pycaw import IAudioEndpointVolume  # type: ignore
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                vol = cast(iface, POINTER(IAudioEndpointVolume))
            vol.SetMasterVolumeLevelScalar(lv / 100.0, None)
            return _ok(f"Volume set to {lv}%")
        except Exception:
            pass
        gui, err = _gui()
        if err:
            return _fail(err)
        try:
            gui.press("volumedown", presses=50, interval=0.01)
            gui.press("volumeup", presses=lv // 2, interval=0.01)
            return _ok(f"Volume set to about {lv}%")
        except Exception as e:
            return _fail(f"Volume change failed: {e}")
    if IS_MAC:
        r = _run(["osascript", "-e", f"set volume output volume {lv}"])
        return _ok(f"Volume set to {lv}%") if r.returncode == 0 else _fail(r.stderr.strip() or "osascript failed")
    # Linux / WSL
    try:
        if shutil.which("pactl"):
            r = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{lv}%"])
            if r.returncode == 0:
                return _ok(f"Volume set to {lv}%")
        if shutil.which("amixer"):
            r = _run(["amixer", "-q", "sset", "Master", f"{lv}%"])
            if r.returncode == 0:
                return _ok(f"Volume set to {lv}%")
    except Exception as e:
        return _fail(f"Volume change failed: {e}")
    return _fail("No volume control available (install pactl or amixer)")

# --------------------------------------------------------------------------- screen

def screenshot() -> dict:
    path = Path(config.TMP_DIR) / "screen.png"
    try:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=False)
        except Exception:
            gui, err = _gui()
            if err:
                return _fail(err)
            img = gui.screenshot()
        # keep payload small for vision model
        img.thumbnail((1600, 1600))
        img.save(path)
        return _ok(str(path))
    except Exception as e:
        return _fail(f"Screenshot failed: {e}")


def read_screen(question: str = "Describe what is on the screen.") -> dict:
    shot = screenshot()
    if not shot["ok"]:
        return shot
    try:
        from .sarvam_client import vision_read
        ans = vision_read(shot["result"], question or "Describe what is on the screen.")
        return _ok(ans)
    except Exception as e:
        return _fail(f"Could not read screen: {e}")


def read_clipboard() -> dict:
    try:
        import pyperclip
        txt = pyperclip.paste() or ""
        return _ok(txt[:4000] if txt else "(clipboard is empty)")
    except Exception as e:
        return _fail(f"Clipboard unavailable: {e}")

# --------------------------------------------------------------------------- sarvam-backed

def translate_text(text: str = "", src: str = "auto", tgt: str = "en-IN") -> dict:
    if not text:
        return _fail("No text given")
    try:
        from .sarvam_client import translate
        return _ok(translate(text, src or "auto", tgt or "en-IN"))
    except Exception as e:
        return _fail(f"Translation failed: {e}")


def speak(text: str = "", lang: str = None) -> dict:
    if not text:
        return _fail("No text given")
    try:
        from .sarvam_client import tts
        from .audio_io import play_audio
        audio = tts(text, lang or config.DEFAULT_LANG)
        play_audio(audio)
        return _ok("Spoke the text aloud")
    except Exception as e:
        return _fail(f"Speech failed: {e}")

# --------------------------------------------------------------------------- misc

def get_time() -> dict:
    now = datetime.datetime.now()
    return _ok(now.strftime("%A, %d %B %Y, %I:%M %p"))


def wait(seconds=1) -> dict:
    try:
        s = max(0.0, min(5.0, float(seconds)))
    except Exception:
        return _fail("seconds must be a number")
    time.sleep(s)
    return _ok(f"Waited {s:g} seconds")

# --------------------------------------------------------------------------- registry

def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


_S = {"type": "string"}
TOOL_SCHEMAS = [
    _fn("open_app", "Open/launch an application on the laptop by name (e.g. chrome, notepad, calculator, vscode, spotify, settings, word, excel, file explorer).",
        {"name": {**_S, "description": "Application name"}}, ["name"]),
    _fn("open_url", "Open a website URL in the default browser.", {"url": _S}, ["url"]),
    _fn("search_web", "Search the web. Use site='youtube' to search YouTube videos/songs, otherwise Google.",
        {"query": _S, "site": {"type": "string", "enum": ["google", "youtube"], "description": "Default google"}}, ["query"]),
    _fn("type_text", "Type text into the currently focused window. Set press_enter true to submit.",
        {"text": _S, "press_enter": {"type": "boolean", "default": False}}, ["text"]),
    _fn("press_key", "Press a single keyboard key (enter, esc, tab, space, up, down, left, right, backspace, f5, playpause, volumemute...).",
        {"key": _S}, ["key"]),
    _fn("hotkey", "Press a key combination, e.g. ['ctrl','t'] for new tab, ['ctrl','s'] to save.",
        {"keys": {"type": "array", "items": _S}}, ["keys"]),
    _fn("scroll", "Scroll the active window. Positive = up, negative = down (units ~ mouse wheel notches, e.g. -5).",
        {"amount": {"type": "integer"}}, ["amount"]),
    _fn("set_volume", "Set system speaker volume 0-100.",
        {"level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["level"]),
    _fn("screenshot", "Take a screenshot; returns the saved image path."),
    _fn("read_screen", "Look at the screen and answer a question about what is visible (reads text, describes UI).",
        {"question": _S}, ["question"]),
    _fn("translate_text", "Translate text between Indian languages / English. Language codes like hi-IN, ta-IN, en-IN.",
        {"text": _S, "src": {**_S, "description": "Source code or 'auto'"}, "tgt": _S}, ["text", "tgt"]),
    _fn("speak", "Speak text aloud in a given language (e.g. hi-IN).", {"text": _S, "lang": _S}, ["text"]),
    _fn("read_clipboard", "Read the current clipboard text."),
    _fn("get_time", "Get the current local date and time."),
    _fn("wait", "Wait a few seconds (max 5) e.g. for a page to load.",
        {"seconds": {"type": "number", "maximum": 5}}, ["seconds"]),
    _fn("close_window", "Close the currently active window (asks for confirmation)."),
]

_REGISTRY = {
    "open_app": open_app, "open_url": open_url, "search_web": search_web,
    "type_text": type_text, "press_key": press_key, "hotkey": hotkey, "scroll": scroll,
    "set_volume": set_volume, "screenshot": screenshot, "read_screen": read_screen,
    "translate_text": translate_text, "speak": speak, "read_clipboard": read_clipboard,
    "get_time": get_time, "wait": wait, "close_window": close_window,
}


def execute_tool(name, args=None, user_command: str = "") -> dict:
    global _preconfirmed
    from . import guardrails
    res = None
    try:
        fn = _REGISTRY.get(str(name))
        if fn is None:
            res = _fail(f"Unknown tool '{name}'")
            return res
        if isinstance(args, str):
            import json
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                return _fail("Arguments were not valid JSON")
        if not isinstance(args, dict):
            args = {}
        import inspect
        params = inspect.signature(fn).parameters
        clean = {k: v for k, v in args.items() if k in params}
        dec = guardrails.check(name, clean, user_command or "")
        if not dec.get("allow"):
            res = _fail(f"Blocked by guardrail: {dec.get('reason', '')}")
            return res
        approved = False
        if dec.get("confirm"):
            try:
                approved = bool(_confirm_cb(f"{name} {clean} -- {dec.get('reason', '')}. Allow?"))
            except Exception:
                approved = False
            if not approved:
                res = DECLINED
                return res
        _preconfirmed = approved
        try:
            out = fn(**clean)
        finally:
            _preconfirmed = False
        if not isinstance(out, dict) or "ok" not in out:
            res = _ok(out)
            return res
        out["result"] = str(out.get("result", ""))
        res = out
        return res
    except Exception as e:  # never raise
        res = _fail(f"{name} failed: {type(e).__name__}: {e}")
        return res
    finally:
        guardrails.audit({"tool": str(name), "args": args, "command": user_command,
                          "ok": (res or {}).get("ok"), "result": str((res or {}).get("result", ""))[:300]})
