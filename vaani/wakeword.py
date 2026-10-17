"""Wake word without any local AI model.

A local energy VAD (numpy RMS) captures each spoken utterance to a WAV; the
caller sends it to Sarvam cloud STT and checks the transcript with
`contains_wake()`. The mic stream is only open inside `wait_for_utterance()`.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from difflib import SequenceMatcher

from . import audio_io, config

WAKE_WAV = config.TMP_DIR / "wake.wav"

# Latin + Devanagari (and a few other Indic scripts) spellings STT may produce.
_NAME_VARIANTS = {
    "vaani", "vani", "vaany", "vaanee", "vanee", "vany", "waani", "wani", "bani", "baani",
    "vahni", "vaane", "vanni", "vaanii", "vaniy",
    "वाणी", "वाणि", "वानी", "वाणि", "वानि", "बानी", "वाणी।", "ভাণী", "বাণী", "வாணி", "வானி",
    "వాణి", "ವಾಣಿ", "വാണി", "વાણી", "ਵਾਣੀ", "ଵାଣୀ", "ବାଣୀ",
}
_HEY = {"hey", "hay", "he", "hi", "hei", "ay", "a", "ok", "okay", "o", "हे", "है", "ए", "हाय", "ओके", "অ্যা", "ஹே", "హే", "ಹೇ", "ഹേ", "હે", "ਹੇ"}


def _norm(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text or "").lower()
    # strip punctuation (keep letters, digits, combining marks of any script)
    text = "".join(ch if (ch.isalnum() or unicodedata.category(ch).startswith("M") or ch.isspace()) else " "
                   for ch in text)
    return text.split()


def _is_name(tok: str) -> bool:
    if tok in _NAME_VARIANTS:
        return True
    if tok.isascii() and 3 <= len(tok) <= 7 and tok not in {"vanity", "vanilla", "vain", "van", "vans", "bank", "banish", "bandi", "wanna"}:
        best = max(SequenceMatcher(None, tok, v).ratio() for v in ("vaani", "vani", "baani"))
        return best >= 0.75
    return False


def contains_wake(text: str) -> tuple[bool, str]:
    """Fuzzy-match a wake phrase at the start of a transcript.
    Returns (matched, remaining_command_text)."""
    raw_tokens = (text or "").split()
    toks = _norm(text)
    if not toks:
        return False, ""
    # wake phrase may appear within the first 3 tokens ("hey", "ok hey vaani", "हे वाणी")
    idx = None
    for i in range(min(3, len(toks))):
        if _is_name(toks[i]) and all(t in _HEY for t in toks[:i]):
            idx = i
            break
        # "heyvaani" / "hevaani" glued together
        m = re.match(r"^(hey|hay|hi|he)(.{3,7})$", toks[i])
        if m and _is_name(m.group(2)) and all(t in _HEY for t in toks[:i]):
            idx = i
            break
    if idx is None:
        return False, ""
    # map back to the original (punctuated) text to keep the command readable
    rest = " ".join(raw_tokens[idx + 1:]) if len(raw_tokens) == len(toks) else " ".join(toks[idx + 1:])
    rest = rest.lstrip(" ,.!?।:;-—").strip()
    return True, rest


class WakeListener:
    """Captures utterances with local energy VAD for cloud STT wake checking."""

    def __init__(self, phrase: str = config.WAKE_WORD, max_sec: float = config.MAX_CLIP_SEC,
                 silence_sec: float = config.SILENCE_SEC):
        self.phrase = phrase
        self.max_sec = max_sec
        self.silence_sec = silence_sec

    def wait_for_utterance(self, stop_event: threading.Event | None = None) -> str | None:
        """Block until one utterance is captured; return its WAV path.
        Returns None if stop_event is set. Raises audio_io.AudioUnavailable."""
        while True:
            chunks = audio_io.vad_capture(self.max_sec, self.silence_sec, no_speech_timeout=None,
                                          stop_event=stop_event)
            if chunks is None:
                return None
            if sum(len(c) for c in chunks) / audio_io.SR >= config.MIN_CLIP_SEC:
                return audio_io._save(chunks, WAKE_WAV)
