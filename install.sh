#!/usr/bin/env bash
# Vaani installer for macOS / Linux / WSL
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first." >&2; exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "Python 3.10+ required (found $("$PY" --version 2>&1))." >&2; exit 1
fi

case "$(uname -s)" in
  Linux)
    echo "Linux: if audio/automation fails, install system deps:"
    echo "  sudo apt install libportaudio2 python3-tk python3-dev scrot xclip"
    if grep -qi microsoft /proc/version 2>/dev/null; then
      echo "WSL detected: audio needs WSLg (Windows 11) or PulseAudio; laptop control acts on the Linux side."
    fi
    ;;
  Darwin)
    echo "macOS: grant your terminal Microphone and Accessibility permissions"
    echo "  (System Settings > Privacy & Security > Microphone / Accessibility)."
    ;;
esac

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

echo "Installed. Running onboarding..."
.venv/bin/vaani setup
echo "Done. Start with: .venv/bin/vaani   (or: source .venv/bin/activate && vaani)"
