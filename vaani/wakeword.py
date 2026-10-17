"""Wake word "Hey Vaani" -- fully local, no API call, no downloaded AI model.

Personalized keyword spotting with classic DSP:
  * energy VAD (numpy RMS) captures each utterance (`WakeListener`),
  * numpy-only MFCC (pre-emphasis, 25 ms/10 ms Hamming frames, FFT, 26 mel filters,
    log, DCT -> 13 coeffs, cepstral mean normalization),
  * DTW (Sakoe-Chiba band, length-normalized, open end) against templates the user
    recorded once with `enroll()` (saved to ~/.vaani/wake_templates.npz).
Only utterances that match are ever sent to Sarvam STT (as the command).
`contains_wake()` is kept to strip a spoken wake phrase from STT text.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from difflib import SequenceMatcher

import numpy as np

from . import audio_io, config

WAKE_WAV = config.TMP_DIR / "wake.wav"
WAKE_CMD_WAV = config.TMP_DIR / "wake_cmd.wav"
TEMPLATES_PATH = config.HOME_DIR / "wake_templates.npz"
SR = config.SAMPLE_RATE
HOP_SEC = 0.010
THRESH_SCALE = 1.35
THRESH_FLOOR = 18.0   # min sanity threshold (normalized DTW units)
THRESH_CAP = 60.0     # never accept anything this far away

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
    """Captures utterances with local energy VAD; check them locally with `matches()`."""

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


# ---------------------------------------------------------------- DSP (numpy only)
def _mel_fb(n_fft: int, sr: int, n_mels: int = 26) -> np.ndarray:
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    pts = mel2hz(np.linspace(hz2mel(20.0), hz2mel(sr / 2), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for m in range(1, n_mels + 1):
        lo, c, r = bins[m - 1], bins[m], bins[m + 1]
        c = max(c, lo + 1)
        r = min(max(r, c + 1), n_fft // 2 + 1)
        fb[m - 1, lo:c] = (np.arange(lo, c) - lo) / (c - lo)
        fb[m - 1, c:r] = (r - np.arange(c, r)) / max(1, r - c)
    return fb


_N_FFT = 512
_WIN = int(0.025 * SR)
_HOP = int(HOP_SEC * SR)
_FB = _mel_fb(_N_FFT, SR)
_DCT = np.cos(np.pi / 26 * (np.arange(26)[None, :] + 0.5) * np.arange(13)[:, None])  # 13x26


def _frame_idx(n_samples: int) -> np.ndarray:
    n = max(1, 1 + (n_samples - _WIN) // _HOP)
    return np.arange(_WIN)[None, :] + _HOP * np.arange(n)[:, None]


def _frames_energy(x: np.ndarray) -> np.ndarray:
    if len(x) < _WIN:
        x = np.pad(x, (0, _WIN - len(x)))
    return np.sqrt(np.mean(x[_frame_idx(len(x))].astype(np.float64) ** 2, axis=1) + 1e-12)


def mfcc(x: np.ndarray, cmn: bool = True) -> np.ndarray:
    """(frames, 13) MFCC of float audio at SR, numpy only."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < _WIN:
        x = np.pad(x, (0, _WIN - len(x)))
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])  # pre-emphasis
    frames = x[_frame_idx(len(x))] * np.hamming(_WIN)
    pspec = np.abs(np.fft.rfft(frames, _N_FFT)) ** 2 / _N_FFT
    feats = np.log(pspec @ _FB.T + 1e-10) @ _DCT.T
    if cmn:
        feats = feats - feats.mean(axis=0)
    return feats


def _energy_thr(e: np.ndarray) -> float:
    return float(max(e.max() * 0.15, np.percentile(e, 10) * 2.5))


def _trim(x: np.ndarray, lead_only: bool = False) -> tuple[np.ndarray, int]:
    """Trim low-energy edges. Returns (trimmed, start_sample)."""
    e = _frames_energy(x)
    on = np.where(e > _energy_thr(e))[0]
    if len(on) == 0:
        return x, 0
    a = max(0, (on[0] - 2) * _HOP)
    b = len(x) if lead_only else min(len(x), (on[-1] + 3) * _HOP + _WIN)
    return x[a:b], a


def _dtw_last_row(t: np.ndarray, u: np.ndarray, band: int) -> np.ndarray:
    """Cumulative DTW cost aligning ALL of template t to every prefix of u
    (Sakoe-Chiba band around the diagonal). Returns D[len(t)-1, :]."""
    L, M = len(t), len(u)
    cost = np.sqrt(((t[:, None, :] - u[None, :, :]) ** 2).sum(-1))
    inf = np.inf
    prev = np.full(M, inf)
    slope = M / max(L, 1)
    for i in range(L):
        row = np.full(M, inf)
        lo = max(0, int(i * min(1.0, slope)) - band)
        hi = min(M, int(i * max(1.0, slope)) + band + 1)
        ci = cost[i]
        if i == 0:
            row[:hi] = np.cumsum(ci[:hi])
        else:
            # diagonal / vertical candidates vectorized, horizontal sequential
            cand = prev[lo:hi].copy()
            if lo > 0:
                cand = np.minimum(cand, prev[lo - 1:hi - 1])
            else:
                cand[1:] = np.minimum(cand[1:], prev[0:hi - 1])
            left = inf
            for k, j in enumerate(range(lo, hi)):
                b = cand[k] if cand[k] < left else left
                left = ci[j] + b
                row[j] = left
        prev = row
    return prev


def _match_one(tpl: np.ndarray, uraw: np.ndarray) -> tuple[float, int]:
    """Best normalized DTW distance of template vs utterance prefixes of 0.8x-1.3x the
    template length. Returns (score, end_frame)."""
    L = len(tpl)
    lo, hi = max(2, int(0.8 * L)), int(1.3 * L) + 1
    u = uraw[:hi]
    if len(u) < lo:
        return float("inf"), 0
    u = u - u[:min(len(u), L)].mean(axis=0)  # CMN over the wake-phrase-sized window
    last = _dtw_last_row(tpl, u, band=max(8, int(0.3 * L)))
    j = np.arange(lo - 1, min(hi, len(u)))
    norm = last[j] / (L + j + 1)
    k = int(np.argmin(norm))
    return float(norm[k]), int(j[k] + 1)


# ---------------------------------------------------------------- templates / enrollment
def load_wav(path) -> np.ndarray:
    """Load a WAV as mono float32 [-1,1] at SR (linear resample if needed)."""
    sf = audio_io._sf()
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    x = data.mean(axis=1)
    if sr != SR and len(x):
        n = int(round(len(x) * SR / sr))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def build_templates(clips: list[np.ndarray]) -> dict:
    """MFCC templates + threshold (mean pairwise DTW * 1.35, floored) from float clips at SR."""
    tpls = [mfcc(_trim(c)[0]) for c in clips]
    dists = []
    for i in range(len(tpls)):
        for j in range(len(tpls)):
            if i != j:
                d, _ = _match_one(tpls[i], mfcc(_trim(clips[j])[0], cmn=False))
                if np.isfinite(d):
                    dists.append(d)
    mean = float(np.mean(dists)) if dists else THRESH_FLOOR
    thr = float(min(THRESH_CAP, max(THRESH_FLOOR, mean * THRESH_SCALE)))
    return {"templates": tpls, "threshold": thr, "pairwise_mean": mean}


def save_templates(data: dict, path=None) -> None:
    arrs = {f"t{i}": t for i, t in enumerate(data["templates"])}
    np.savez(str(path or TEMPLATES_PATH), threshold=np.array(data["threshold"]), **arrs)


def load_templates(path=None) -> dict | None:
    try:
        z = np.load(str(path or TEMPLATES_PATH))
        keys = sorted((k for k in z.files if k[0] == "t" and k[1:].isdigit()), key=lambda k: int(k[1:]))
        if not keys:
            return None
        return {"templates": [z[k] for k in keys], "threshold": float(z["threshold"])}
    except Exception:
        return None


def is_enrolled() -> bool:
    return load_templates() is not None


def enroll(n: int = 3, on_prompt=None) -> dict:
    """Record n samples of the wake phrase (each 0.3-3.0 s), save templates + threshold.
    on_prompt(msg) is called before each recording / on retry (default: print)."""
    say = on_prompt or print
    clips: list[np.ndarray] = []
    tries = 0
    while len(clips) < n and tries < n + 5:
        tries += 1
        say(f"Say 'Hey Vaani' ({len(clips) + 1}/{n})...")
        try:
            wav = audio_io.record_until_silence(max_sec=4.0, silence_sec=0.6, no_speech_timeout=6.0)
        except audio_io.ClipTooShort as e:
            say(f"  didn't hear it ({e}) - speak a bit louder / closer, try again")
            continue
        raw = load_wav(wav)
        x, _ = _trim(raw)
        dur = len(x) / SR
        peak = float(np.abs(raw).max()) if raw.size else 0.0
        if peak < 0.02:
            say(f"  too quiet (peak {peak:.3f}) - check the mic, try again")
            continue
        if dur < 0.3:
            say(f"  too short ({dur:.2f}s, peak {peak:.2f}) - say 'Hey Vaani', try again")
            continue
        if dur > 3.0:
            say(f"  too long ({dur:.1f}s, peak {peak:.2f}) - say just 'Hey Vaani', try again")
            continue
        clips.append(x)
    if len(clips) < n:
        raise RuntimeError("Enrollment incomplete (not enough clear samples).")
    data = build_templates(clips)
    save_templates(data)
    return data


def matches_audio(x: np.ndarray, data: dict | None = None) -> tuple[bool, float, float]:
    """Local wake check on float audio at SR. Returns (matched, score, cut_sec);
    cut_sec = where the wake phrase ends in x (trailing audio = command)."""
    data = data or load_templates()
    if not data or len(x) < _WIN:
        return False, float("inf"), 0.0
    xt, start = _trim(x, lead_only=True)
    u = mfcc(xt, cmn=False)
    best, best_end = float("inf"), 0
    for tpl in data["templates"]:
        d, end = _match_one(tpl, u)
        if d < best:
            best, best_end = d, end
    cut = (start + best_end * _HOP + (_WIN - _HOP)) / SR
    return bool(best <= data["threshold"]), float(best), float(min(cut, len(x) / SR))


def matches(wav_path, data: dict | None = None) -> tuple[bool, float, float]:
    """Local wake check of a WAV (no network). Returns (matched, score, cut_sec)."""
    return matches_audio(load_wav(wav_path), data)


def speech_after(wav_path, cut_sec: float) -> float:
    """Seconds of voiced audio after cut_sec (energy threshold from the whole clip)."""
    full = load_wav(wav_path)
    rest = full[int(cut_sec * SR):]
    if len(rest) < _WIN:
        return 0.0
    thr = _energy_thr(_frames_energy(full))
    return float((_frames_energy(rest) > thr).sum() * HOP_SEC)


def trim_wav(wav_path, cut_sec: float, out=None) -> str:
    """Write the audio after cut_sec to a new WAV (the spoken command) and return its path."""
    x = load_wav(wav_path)[int(cut_sec * SR):]
    out = str(out or WAKE_CMD_WAV)
    audio_io._sf().write(out, x, SR, subtype="PCM_16")
    return out
