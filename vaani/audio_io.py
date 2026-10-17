"""Microphone recording, playback and beeps. Cross-platform (sounddevice/PortAudio).

All device access is lazy so importing this module never fails on machines
without audio hardware (e.g. WSL); callers get AudioUnavailable with a clear message.
"""
from __future__ import annotations

import io
import queue
import threading
import time
from pathlib import Path

import numpy as np

from . import config

SR = config.SAMPLE_RATE
INPUT_WAV = config.TMP_DIR / "input.wav"
BLOCK = 1600  # 100 ms at 16 kHz


class ClipTooShort(Exception):
    """Recorded clip was too short or contained no speech."""


class AudioUnavailable(RuntimeError):
    """No usable audio backend/device."""


def _sd():
    try:
        import sounddevice as sd  # noqa
        return sd
    except (ImportError, OSError) as e:
        hint = ""
        if config.IS_WSL:
            hint = " WSL usually has no audio devices; run Vaani natively on Windows, or set up WSLg/PulseAudio."
        elif config.OS_NAME == "Linux":
            hint = " On Linux install PortAudio: sudo apt install libportaudio2."
        raise AudioUnavailable(f"Audio backend unavailable ({e}).{hint}") from e


def _sf():
    try:
        import soundfile as sf  # noqa
        return sf
    except (ImportError, OSError) as e:
        raise AudioUnavailable(f"soundfile/libsndfile unavailable ({e}). pip install soundfile") from e


def _check_input_device(sd) -> None:
    try:
        sd.query_devices(kind="input")
    except Exception as e:
        hint = " (WSL has no microphone by default)" if config.IS_WSL else ""
        raise AudioUnavailable(f"No microphone found{hint}: {e}") from e


def open_input_stream(callback, blocksize: int = BLOCK):
    """Open (not started) a 16k mono int16 InputStream. Raises AudioUnavailable."""
    sd = _sd()
    _check_input_device(sd)
    try:
        return sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                              blocksize=blocksize, callback=callback)
    except Exception as e:
        raise AudioUnavailable(f"Could not open microphone: {e}") from e


def _save(chunks: list[np.ndarray], path: Path = INPUT_WAV) -> str:
    sf = _sf()
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    audio = audio.reshape(-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, SR, subtype="PCM_16")
    return str(path)


def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    f = block.astype(np.float32)
    return float(np.sqrt(np.mean(f * f)))


# ---------------------------------------------------------------- key handling
def _key_matches(key, want: str) -> bool:
    want = want.lower().strip()
    name = getattr(key, "name", None)  # special keys: f9, space, ctrl_r ...
    if name and name.lower() == want:
        return True
    ch = getattr(key, "char", None)
    return bool(ch) and ch.lower() == want


def wait_key_press(key: str = config.PTT_KEY, stop_event: threading.Event | None = None) -> bool:
    """Block until `key` is pressed. Returns False if stop_event set first."""
    from pynput import keyboard
    pressed = threading.Event()

    def on_press(k):
        if _key_matches(k, key):
            pressed.set()
            return False

    with keyboard.Listener(on_press=on_press) as lst:
        while not pressed.is_set():
            if stop_event is not None and stop_event.is_set():
                lst.stop()
                return False
            pressed.wait(0.05)
    return True


def record_while_held(key: str = config.PTT_KEY, already_pressed: bool = True,
                      released: threading.Event | None = None) -> str:
    """Record while `key` stays held (assumes it is currently held if already_pressed).
    If `released` is given, an external listener signals key release through it."""
    from pynput import keyboard
    if not already_pressed:
        wait_key_press(key)
    lst = None
    if released is None:
        released = threading.Event()

        def on_release(k):
            if _key_matches(k, key):
                released.set()
                return False

        lst = keyboard.Listener(on_release=on_release)
        lst.start()

    q: queue.Queue = queue.Queue()
    chunks: list[np.ndarray] = []
    t0 = time.monotonic()
    try:
        stream = open_input_stream(lambda indata, frames, t, status: q.put(indata.copy()))
        with stream:
            while not released.is_set() and time.monotonic() - t0 < config.MAX_CLIP_SEC:
                try:
                    chunks.append(q.get(timeout=0.05))
                except queue.Empty:
                    pass
    finally:
        if lst is not None:
            lst.stop()
    while not q.empty():
        chunks.append(q.get_nowait())
    dur = sum(len(c) for c in chunks) / SR
    if dur < config.MIN_CLIP_SEC:
        raise ClipTooShort(f"Clip too short ({dur:.2f}s). Hold {key.upper()} while speaking.")
    return _save(chunks)


def record_while_key_held(key: str = config.PTT_KEY) -> str:
    """Block until key pressed, record while held, stop on release. Returns wav path."""
    _check_input_device(_sd())  # fail fast before waiting on the key
    wait_key_press(key)
    return record_while_held(key, already_pressed=True)


# ---------------------------------------------------------------- VAD recording
def _threshold(floor: float) -> float:
    return max(floor * 3.0, floor + 150.0, 300.0)


def vad_capture(max_sec: float = config.MAX_CLIP_SEC,
                silence_sec: float = config.SILENCE_SEC,
                no_speech_timeout: float | None = 5.0,
                stop_event: threading.Event | None = None,
                min_speech_sec: float = 0.25,
                preroll_sec: float = 0.3) -> list[np.ndarray] | None:
    """Energy-VAD capture of one utterance (pure numpy, no model).

    Calibrates the noise floor over the first 0.3 s (then tracks it slowly while
    idle), starts on speech, ends after `silence_sec` of silence or `max_sec` of
    speech. Keeps a short pre-roll so the first syllable isn't clipped.
    Returns the chunks, or None if stopped / timed out with no speech.
    The mic stream is closed on return."""
    q: queue.Queue = queue.Queue()
    stream = open_input_stream(lambda indata, frames, t, status: q.put(indata.copy()))
    block_sec = BLOCK / SR
    calib_blocks = max(1, int(0.3 / block_sec))
    preroll_blocks = max(1, int(preroll_sec / block_sec))
    noise: list[float] = []
    floor = None
    pre: list[np.ndarray] = []
    chunks: list[np.ndarray] = []
    speaking = False
    loud_run = 0
    speech_sec = 0.0
    silent_run = 0.0
    idle = 0.0
    with stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            try:
                blk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            level = _rms(blk)
            if floor is None:
                noise.append(level)
                if len(noise) >= calib_blocks:
                    floor = float(np.median(noise))
                continue
            loud = level > _threshold(floor)
            if not speaking:
                pre.append(blk)
                if len(pre) > preroll_blocks + 2:
                    pre.pop(0)
                if loud:
                    loud_run += 1
                    if loud_run >= 2:  # ~200 ms of energy -> speech onset
                        speaking = True
                        chunks = list(pre)
                        speech_sec = loud_run * block_sec
                        silent_run = 0.0
                else:
                    loud_run = 0
                    floor = 0.95 * floor + 0.05 * level  # adapt to ambient changes
                    idle += block_sec
                    if no_speech_timeout is not None and idle >= no_speech_timeout:
                        return None
                continue
            chunks.append(blk)
            if loud:
                speech_sec += block_sec
                silent_run = 0.0
            else:
                silent_run += block_sec
            total = len(chunks) * block_sec
            if silent_run >= silence_sec or total >= max_sec:
                if speech_sec < min_speech_sec:  # a click/bump, keep listening
                    if no_speech_timeout is not None:
                        return None
                    speaking, loud_run, chunks, pre = False, 0, [], []
                    continue
                return chunks


def record_until_silence(max_sec: float = config.MAX_CLIP_SEC,
                         silence_sec: float = config.SILENCE_SEC,
                         no_speech_timeout: float = 5.0) -> str:
    """Record until energy VAD sees `silence_sec` of silence after speech. Saves input.wav."""
    chunks = vad_capture(max_sec, silence_sec, no_speech_timeout)
    if not chunks:
        raise ClipTooShort("No speech detected.")
    dur = sum(len(c) for c in chunks) / SR
    if dur < config.MIN_CLIP_SEC:
        raise ClipTooShort(f"Clip too short ({dur:.2f}s).")
    return _save(chunks)


# ---------------------------------------------------------------- playback
def play_audio(path_or_bytes) -> None:
    """Play a WAV (path or raw bytes), blocking until finished."""
    sd, sf = _sd(), _sf()
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else str(path_or_bytes)
    data, sr = sf.read(src, dtype="float32", always_2d=False)
    try:
        sd.play(data, sr)
        sd.wait()
    except Exception as e:
        raise AudioUnavailable(f"Playback failed: {e}") from e


_TONES = {
    "listen": [(660, 0.08), (880, 0.10)],
    "done": [(880, 0.08), (660, 0.10)],
    "error": [(300, 0.15), (220, 0.20)],
}


def beep(kind: str = "listen") -> None:
    """Short synthesized tone. Never raises."""
    try:
        sd = _sd()
        sr = 22050
        parts = []
        for freq, dur in _TONES.get(kind, _TONES["listen"]):
            t = np.arange(int(sr * dur)) / sr
            tone = 0.25 * np.sin(2 * np.pi * freq * t)
            fade = min(len(t) // 4, int(sr * 0.01))
            if fade:
                env = np.ones_like(tone)
                env[:fade] = np.linspace(0, 1, fade)
                env[-fade:] = np.linspace(1, 0, fade)
                tone *= env
            parts.append(tone)
            parts.append(np.zeros(int(sr * 0.02)))
        sd.play(np.concatenate(parts).astype(np.float32), sr)
        sd.wait()
    except Exception:
        pass


# ---------------------------------------------------------------- onboarding helpers
def list_input_devices() -> list[str]:
    try:
        sd = _sd()
        return [f"{i}: {d['name']}" for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0) > 0]
    except Exception:
        return []


def mic_check(seconds: float = 1.5) -> float:
    """Record briefly; return peak level 0..1. Raises AudioUnavailable."""
    sd = _sd()
    _check_input_device(sd)
    try:
        rec = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16")
        sd.wait()
    except Exception as e:
        raise AudioUnavailable(f"Mic check failed: {e}") from e
    return float(np.abs(rec.astype(np.int32)).max() / 32768.0) if rec.size else 0.0
