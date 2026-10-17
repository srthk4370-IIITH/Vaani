"""Safety guardrails for Vaani tool execution.

check(tool_name, args, user_command) -> {"allow": bool, "confirm": bool, "reason": str}
  Layer 1: instant local rules.  Layer 2: Sarvam cloud risk review (optional, VAANI_CLOUD_GUARD).
audit(event) -> appends JSONL to HOME_DIR/history.jsonl; never raises.
"""
from __future__ import annotations

import datetime
import json
import re
import threading

from . import config

READ_ONLY = {"screenshot", "read_screen", "read_clipboard", "get_time", "translate_text",
             "speak", "scroll", "wait", "search_web", "open_url",
             "transliterate_text", "detect_language", "read_image_text", "ask_sarvam", "read_document",
             "system_info"}
# Local, harmless actions that need no cloud review.
SAFE_LOCAL = {"set_volume", "media_control", "set_brightness"}
EXEC_EXT_RE = re.compile(
    r"\.(exe|bat|cmd|ps1|psm1|sh|msi|vbs|vbe|js|jse|wsf|scr|com|pif|cpl|msc|hta|jar|app|command|lnk|"
    r"reg|py|pyw|run|bin|appimage|desktop|url)\s*$", re.I)

DELETE_INTENT_RE = re.compile(
    r"\b(delete|deleting|remove|removing|erase|hatao|hata\s*do|mitao|mita\s*do|uninstall|"
    r"empty\s+(the\s+)?(trash|recycle\s*bin)|format|kill|terminate|end\s+task)\b", re.I)

DESTRUCTIVE_RE = re.compile(
    r"\b(delete|del|erase|remove|rm|rmdir|rd|format|uninstall|wipe|shred|"
    r"empty\s+(the\s+)?(trash|recycle\s*bin)|taskkill|kill(all)?|pkill|terminate|"
    r"clear-recyclebin|remove-item)\b", re.I)

SHELL_DANGER_RE = re.compile(
    r"(rm\s+-[a-z]*[rf]|format\s+[a-z]:|del\s+/[sqf]|rd\s+/s|rmdir\s+/s|\bshutdown\b|"
    r"reg\s+delete|powershell[^\n]*\s-e(nc(odedcommand)?)?\s|\biex\b|invoke-expression|"
    r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh|mkfs|dd\s+if=|:\(\)\s*\{|diskpart|bcdedit|"
    r"vssadmin\s+delete|cipher\s+/w|remove-item[^\n]*-recurse|takeown|icacls[^\n]*/grant)", re.I)

COMMIT_RE = re.compile(
    r"\b(pay|payment|purchase|buy|order|checkout|send|submit|post|transfer|place\s+order|"
    r"book\s+now|confirm\s+order|email)\b", re.I)

DESTRUCTIVE_HOTKEYS = {"shift+delete", "ctrl+shift+delete", "command+delete", "command+backspace",
                       "ctrl+shift+esc", "alt+f4", "command+q", "ctrl+q"}

_cache: dict = {}
_cache_lock = threading.Lock()


def _d(allow, confirm, reason):
    return {"allow": bool(allow), "confirm": bool(confirm), "reason": str(reason)}


def _norm_keys(keys):
    if isinstance(keys, str):
        keys = re.split(r"[+\s,]+", keys)
    alias = {"control": "ctrl", "del": "delete", "return": "enter", "cmd": "command"}
    return [alias.get(str(k).strip().lower(), str(k).strip().lower()) for k in (keys or []) if str(k).strip()]


def _local(tool, args, cmd):
    """Return a decision dict, or None if undecided (-> cloud review)."""
    has_del_intent = bool(DELETE_INTENT_RE.search(cmd or ""))
    blob = json.dumps(args, ensure_ascii=False, default=str) if args else ""

    if tool in READ_ONLY:
        if tool == "open_url":
            url = str(args.get("url", "")).strip().lower()
            if url and re.match(r"^[a-z][a-z0-9+.\-]*://", url) and not url.startswith(("http://", "https://")):
                return _d(False, False, "only http(s) URLs are allowed")
        return _d(True, False, "read-only")
    if tool in SAFE_LOCAL:
        return _d(True, False, "harmless local action")
    if tool == "open_path":
        p = str(args.get("name_or_path", "")).strip().strip('"')
        if EXEC_EXT_RE.search(p):
            return _d(False, False, "opening programs/scripts is not allowed")
        return _d(True, False, "opening a file/folder")
    if tool == "lock_screen":
        return _d(True, True, "this locks the screen")

    # Shell-dangerous strings typed anywhere: always blocked.
    if tool == "type_text" and SHELL_DANGER_RE.search(str(args.get("text", ""))):
        return _d(False, False, "typing a dangerous shell command is not allowed")

    # Destructive intent in the action itself.
    destructive = False
    if tool == "hotkey":
        keys = _norm_keys(args.get("keys"))
        combo = "+".join(keys)
        destructive = combo in DESTRUCTIVE_HOTKEYS and combo not in ("alt+f4", "command+q", "ctrl+q") \
            or "delete" in keys and len(keys) > 1
    elif tool == "press_key":
        destructive = str(args.get("key", "")).strip().lower() in ("delete", "del")
    elif tool in ("type_text", "open_app"):
        destructive = bool(DESTRUCTIVE_RE.search(blob))
    if destructive:
        if has_del_intent:
            return _d(True, True, "this deletes or removes something")
        return _d(False, False, "deleting/removing/killing is not allowed unless you explicitly ask for it")

    if tool == "close_window":
        return _d(True, bool(config.SAFE_MODE), "closing the window may lose unsaved work")
    if tool == "press_key" and str(args.get("key", "")).strip().lower() in ("enter", "return"):
        return _d(True, bool(config.SAFE_MODE), "pressing Enter may submit something")
    if tool == "hotkey":
        keys = _norm_keys(args.get("keys"))
        combo = "+".join(keys)
        if combo in ("alt+f4", "command+q", "ctrl+q", "ctrl+w", "command+w"):
            return _d(True, bool(config.SAFE_MODE), f"{combo} closes a window/app")
        if "enter" in keys:
            return _d(True, bool(config.SAFE_MODE), f"{combo} may submit something")
    if tool == "type_text":
        if args.get("press_enter"):
            return _d(True, bool(config.SAFE_MODE), "typing then pressing Enter may submit something")
        if COMMIT_RE.search(str(args.get("text", ""))):
            return _d(True, True, "text looks like a payment/send/submit action")
    return None


SYS = ("You are a safety reviewer for a voice assistant that controls a laptop. Given the user's "
       "command and ONE proposed action, rate the risk of the action. high = destructive, irreversible, "
       "deletes/uninstalls/kills, spends money, sends messages/emails, changes security settings, or "
       "clearly NOT what the user asked. medium = could submit/close/lose data but plausibly intended. "
       "low = harmless and consistent with the command. Reply with ONLY JSON: "
       '{"risk":"low|medium|high","reason":"<short>"}')


def _cloud(tool, args, cmd):
    key = (tool, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str), cmd or "")
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    fallback = _d(True, bool(config.SAFE_MODE), "safety review unavailable")
    box = {}

    def work():
        try:
            from . import sarvam_client
            msgs = [{"role": "system", "content": SYS},
                    {"role": "user", "content": f"User command: {cmd or '(none)'}\n"
                                                f"Proposed action: {tool} {key[1]}"}]
            resp = sarvam_client.chat(msgs, model=config.AGENT_MODEL)
            box["text"] = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(getattr(config, "GUARD_TIMEOUT", 10))
    text = re.sub(r"<think>.*?(</think>|$)", "", box.get("text", ""), flags=re.S | re.I)
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return fallback  # not cached: transient
    try:
        v = json.loads(m.group(0))
        risk = str(v.get("risk", "")).lower()
        reason = str(v.get("reason", ""))[:200] or f"{risk} risk"
    except Exception:
        return fallback
    if risk == "high":
        dec = _d(False, False, f"AI safety review: {reason}")
    elif risk == "medium":
        dec = _d(True, True, f"AI safety review: {reason}")
    elif risk == "low":
        dec = _d(True, False, f"AI safety review: {reason}")
    else:
        return fallback
    with _cache_lock:
        _cache[key] = dec
    return dec


def check(tool_name, args=None, user_command: str = "") -> dict:
    try:
        tool = str(tool_name or "")
        args = args if isinstance(args, dict) else {}
        dec = _local(tool, args, user_command or "")
        if dec is not None:
            return dec
        if getattr(config, "CLOUD_GUARD", True) and getattr(config, "SARVAM_API_KEY", ""):
            return _cloud(tool, args, user_command or "")
        return _d(True, False, "no rule matched")
    except Exception as e:
        return _d(True, bool(config.SAFE_MODE), f"guardrail error: {type(e).__name__}")


def audit(event: dict) -> None:
    try:
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        rec.update(event or {})
        with open(config.HOME_DIR / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
