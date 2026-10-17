"""Vaani agent loop: English command -> Sarvam 105B tool-calling loop -> final English message.

Native OpenAI-style tool calling first; if the API reports tools unsupported, switch (sticky,
process-wide) to a JSON-in-prompt fallback.
"""
import json
import re
from collections import deque

from . import config
from . import sarvam_client as sc
from . import tools

RESULT_MAX = 1500
GIVE_UP = "I couldn't finish this."

SYSTEM_PROMPT = (
    "You are Vaani, an agent that controls the user's laptop (OS: {OS}). The user's command is in "
    "English, translated from {lang}. Use the provided tools to accomplish it, one step at a time. "
    "After each tool result, decide the next step. Use read_screen if you need to verify what's on "
    "screen. When done, reply with a short final message (under 20 words). If a tool fails twice, "
    "stop and explain the problem simply. Never invent tools.\n\n"
    "Extra rules:\n"
    "- If the user just asks a simple question or chats (e.g. a fact, greeting), answer directly "
    "without tools.\n"
    "- To search or play something on YouTube, use search_web with site='youtube'.\n"
    "- open_url and search_web already open the default browser; do not call open_app for a browser "
    "first unless the user explicitly asks to open that app (even then, open_url alone is fine).\n"
    "- Never delete, uninstall or remove anything unless the user explicitly asked to.\n"
    "- Call one tool at a time and keep the final message plain text, no markdown."
)

JSON_FALLBACK_SUFFIX = (
    "\n\nYou cannot call tools natively. Available tools (name: description; parameters JSON schema):\n"
    "{tool_list}\n\n"
    "Reply with ONLY one JSON object and nothing else, either\n"
    '{{"tool": "<tool name>", "args": {{...}}}} to call a tool, or\n'
    '{{"final": "<short final message>"}} when done.'
)

LANG_RULE = ("\n- Write the final message in the language {lang} (native script), not English, "
             "unless {lang} is en-IN.")

# ---- session-only conversation memory (in-process, never persisted)
MEMORY_TURNS = 4
MEMORY_MAX_CHARS = 2000
_memory = deque(maxlen=MEMORY_TURNS)  # (user_cmd_en, final_reply, tools_summary)


def reset_memory():
    _memory.clear()


def _remember(cmd, reply, tools_used):
    summ = ""
    if tools_used:
        summ = "[tools used: " + ", ".join(tools_used)[:200] + "]"
    _memory.append((cmd[:600], (reply or "")[:600], summ))


def _history_msgs():
    out, total = [], 0
    for cmd, reply, summ in reversed(_memory):  # newest first, drop oldest when over budget
        a = f"{summ} {reply}".strip() if summ else reply
        n = len(cmd) + len(a)
        if total + n > MEMORY_MAX_CHARS:
            break
        total += n
        out[:0] = [{"role": "user", "content": cmd}, {"role": "assistant", "content": a or "Done."}]
    return out


_json_mode = False  # sticky: set once the API says native tools are unsupported

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


def _strip_think(text) -> str:
    text = _THINK_RE.sub("", text or "")
    # replies are spoken; drop markdown emphasis/code/headers so TTS doesn't read symbols
    text = re.sub(r"[*_`#]+", "", text)
    return text.strip()


def _emit(on_event, kind, data):
    if on_event is None:
        return
    try:
        on_event(kind, data)
    except Exception:
        pass


def _tool_list_text() -> str:
    lines = []
    for s in tools.TOOL_SCHEMAS:
        f = s.get("function", s)
        params = json.dumps(f.get("parameters", {}).get("properties", {}), ensure_ascii=False)
        lines.append(f"- {f.get('name')}: {f.get('description', '')} params={params}")
    return "\n".join(lines)


def _tool_names():
    return {s.get("function", s).get("name") for s in tools.TOOL_SCHEMAS}


def _result_text(res: dict) -> str:
    r = res.get("result")
    s = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, default=str)
    if len(s) > RESULT_MAX:
        s = s[:RESULT_MAX] + "...[truncated]"
    return ("OK: " if res.get("ok") else "FAILED: ") + s


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _parse_json_reply(text: str):
    """Extract the first JSON object from a model reply (code fences tolerated)."""
    t = re.sub(r"```(?:json)?", "", text, flags=re.I).strip()
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    return None


class _Runner:
    def __init__(self, on_event):
        self.on_event = on_event
        self.fail_counts = {}
        self.user_command = ""
        self.names = _tool_names()
        self.tools_used = []

    def run_tool(self, name, args):
        """Execute a tool; returns (result_text, stop_message_or_None)."""
        _emit(self.on_event, "tool_call", {"name": name, "args": args})
        try:
            brief = json.dumps(args, ensure_ascii=False)[:60]
        except (TypeError, ValueError):
            brief = ""
        self.tools_used.append(f"{name}({brief})")
        if name not in self.names:
            res = {"ok": False, "result": f"Unknown tool '{name}'. Use only the provided tools."}
        else:
            try:
                res = tools.execute_tool(name, args, user_command=self.user_command)
            except Exception as e:  # execute_tool should never raise, but be safe
                res = {"ok": False, "result": f"{type(e).__name__}: {e}"}
            if not isinstance(res, dict):
                res = {"ok": True, "result": res}
        ok = bool(res.get("ok"))
        _emit(self.on_event, "tool_result", {"name": name, "ok": ok, "result": res.get("result")})
        text = _result_text(res)
        if not ok:
            n = self.fail_counts.get(name, 0) + 1
            self.fail_counts[name] = n
            if n >= 2:
                text += (f"\n[{name} has now failed twice. Stop and give a short final message "
                         "explaining the problem simply.]")
            if n >= 3:  # model ignored the instruction: stop ourselves
                return text, f"Sorry, {name.replace('_', ' ')} kept failing: {str(res.get('result'))[:120]}"
        return text, None


def _system(lang):
    lang = lang or "en-IN"
    p = SYSTEM_PROMPT.format(OS=config.OS_NAME, lang=lang)
    if not str(lang).startswith("en"):
        p += LANG_RULE.format(lang=lang)
    return p


def _run_native(command_en, lang, runner):
    """Returns final str, or None if we must switch to JSON mode."""
    global _json_mode
    msgs = [{"role": "system", "content": _system(lang)}, *_history_msgs(),
            {"role": "user", "content": command_en}]
    for _ in range(config.MAX_AGENT_STEPS):
        try:
            resp = sc.chat(msgs, tools=tools.TOOL_SCHEMAS)
        except sc.SarvamError as e:
            if getattr(e, "tools_unsupported", False):
                _json_mode = True
                return None
            raise
        msg = ((resp or {}).get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        content = _strip_think(msg.get("content"))
        if not calls:
            if not content:
                return "Done."
            j = _parse_json_reply(content) if content.lstrip().startswith(("{", "```")) else None
            if j and "tool" in j:
                calls = [{"id": "json_0", "type": "function",
                          "function": {"name": j.get("tool"), "arguments": json.dumps(j.get("args") or {})}}]
            elif j and "final" in j:
                return str(j["final"])
            else:
                return content
        calls = [c for c in calls if isinstance(c, dict)]
        for i, c in enumerate(calls):
            c.setdefault("id", f"call_{len(msgs)}_{i}")
            c.setdefault("type", "function")
        msgs.append({"role": "assistant", "content": content or "", "tool_calls": calls})
        stop = None
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            text, s = runner.run_tool(name, _parse_args(fn.get("arguments")))
            msgs.append({"role": "tool", "tool_call_id": c["id"], "name": name, "content": text})
            stop = stop or s
        if stop:
            return stop
    return GIVE_UP


def _run_json(command_en, lang, runner, steps):
    sys_prompt = _system(lang) + JSON_FALLBACK_SUFFIX.format(tool_list=_tool_list_text())
    msgs = [{"role": "system", "content": sys_prompt}, *_history_msgs(),
            {"role": "user", "content": command_en}]
    for _ in range(steps):
        resp = sc.chat(msgs)
        msg = ((resp or {}).get("choices") or [{}])[0].get("message") or {}
        content = _strip_think(msg.get("content"))
        j = _parse_json_reply(content)
        if j is None:
            return content or "Done."  # plain text = final answer
        if "final" in j and "tool" not in j:
            return str(j["final"]).strip() or "Done."
        name = str(j.get("tool") or "")
        args = j.get("args") if isinstance(j.get("args"), dict) else {}
        msgs.append({"role": "assistant", "content": json.dumps({"tool": name, "args": args}, ensure_ascii=False)})
        text, stop = runner.run_tool(name, args)
        if stop:
            return stop
        msgs.append({"role": "user", "content": f"Tool result for {name}: {text}\nNext step? Reply with JSON only."})
    return GIVE_UP


def run_agent(command_en: str, lang: str, on_event=None) -> str:
    """Run the agent for an English command. Returns a short final English message.

    Raises sc.SarvamError on API failure (caller turns it into a spoken error)."""
    command_en = (command_en or "").strip()
    if not command_en:
        return "I didn't catch that."
    runner = _Runner(on_event)
    runner.user_command = command_en
    out = None
    if not _json_mode:
        out = _run_native(command_en, lang, runner)
    if out is None:
        out = _run_json(command_en, lang, runner, config.MAX_AGENT_STEPS)
    _remember(command_en, out, runner.tools_used)
    return out
