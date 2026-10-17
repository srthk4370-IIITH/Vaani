"""Unified trigger: wait for EITHER a spoken utterance (wake-word candidate) OR the PTT key.

Only one mic stream is open at a time: the VAD stream is stopped (and its thread
joined) before push-to-talk recording opens its own stream.
"""
from __future__ import annotations

import queue
import threading

from . import audio_io, config
from .wakeword import WakeListener

_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        print(f"[vaani] {msg}")
        _warned.add(msg)


def wait_for_command(use_wake: bool = True, ptt_key: str = config.PTT_KEY,
                     on_listen=None) -> tuple[str, str]:
    """Block until PTT key press (records while held) or a VAD-captured utterance.

    Returns (wav_path, source) with source "ptt" or "wake". For "wake" the caller
    runs STT and checks wakeword.contains_wake(); for "ptt" the wav is the command.
    on_listen(source) is called when PTT recording starts / an utterance is captured.
    Raises audio_io.ClipTooShort / audio_io.AudioUnavailable."""
    try:
        from pynput import keyboard
    except Exception as e:  # e.g. headless Linux without X / Wayland restrictions
        keyboard = None
        if not use_wake:
            raise audio_io.AudioUnavailable(f"Push-to-talk unavailable (keyboard hook failed: {e}) and wake word disabled.")
        _warn_once(f"Push-to-talk unavailable ({e}); using wake word only.")

    if keyboard is None:
        path = WakeListener().wait_for_utterance()
        if on_listen:
            on_listen("wake")
        return path, "wake"

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    pressed = threading.Event()
    released = threading.Event()

    def on_press(k):
        if audio_io._key_matches(k, ptt_key) and not pressed.is_set():
            pressed.set()
            stop.set()  # tell the VAD thread to release the mic now
            events.put(("ptt", None))

    def on_release(k):
        if audio_io._key_matches(k, ptt_key) and pressed.is_set():
            released.set()

    kb = keyboard.Listener(on_press=on_press, on_release=on_release)
    kb.start()

    wake_thread = None
    if use_wake:
        def wake_worker():
            try:
                path = WakeListener().wait_for_utterance(stop_event=stop)
                if path:
                    events.put(("wake", path))
            except Exception as e:
                events.put(("wake_error", e))

        wake_thread = threading.Thread(target=wake_worker, daemon=True, name="vaani-vad")
        wake_thread.start()

    try:
        while True:
            src, payload = events.get()
            if src == "wake_error":
                if pressed.is_set():
                    continue
                if isinstance(payload, audio_io.AudioUnavailable):
                    raise payload
                _warn_once(f"Wake listener failed ({payload}); push-to-talk only (hold {ptt_key.upper()}).")
                continue
            break
        stop.set()
        if wake_thread is not None:
            wake_thread.join(timeout=2)  # VAD stream closed before PTT opens one

        if src == "wake":
            if on_listen:
                try:
                    on_listen("wake")
                except Exception:
                    pass
            return payload, "wake"

        if on_listen:
            try:
                on_listen("ptt")
            except Exception:
                pass
        path = audio_io.record_while_held(ptt_key, already_pressed=True, released=released)
        return path, "ptt"
    finally:
        stop.set()
        kb.stop()
