"""Central config. Every Sarvam model ID / endpoint lives here (verify vs docs.sarvam.ai)."""
import os
import platform
from pathlib import Path

HOME_DIR = Path(os.environ.get("VAANI_HOME", Path.home() / ".vaani"))
HOME_DIR.mkdir(parents=True, exist_ok=True)
ENV_PATH = HOME_DIR / ".env"
SETTINGS_PATH = HOME_DIR / "settings.json"
TMP_DIR = HOME_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR = HOME_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

def _load_env(path):
    """Minimal .env reader tolerant of UTF-16 (PowerShell `echo >`) and BOMs."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "ignore")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ENV_PATH)
_load_env(Path.cwd() / ".env")

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# --- Sarvam endpoints / models ---
SARVAM_BASE = "https://api.sarvam.ai"
STT_URL = f"{SARVAM_BASE}/speech-to-text"
STT_TRANSLATE_URL = f"{SARVAM_BASE}/speech-to-text-translate"
TRANSLATE_URL = f"{SARVAM_BASE}/translate"
TTS_URL = f"{SARVAM_BASE}/text-to-speech"
CHAT_URL = f"{SARVAM_BASE}/v1/chat/completions"
TRANSLITERATE_URL = f"{SARVAM_BASE}/transliterate"
TEXT_LID_URL = f"{SARVAM_BASE}/text-lid"
# gemma4 (image input) is served ONLY on V2 chat completions (beta; key needs access). V1 = sarvam-105b only.
CHAT_V2_URL = f"{SARVAM_BASE}/v2/chat/completions"

# Verified vs docs.sarvam.ai (Oct 2026). /speech-to-text-translate is legacy (saaras:v2.5 only);
# saaras:v3/v4 both support mode="translate" on /speech-to-text (docs models/saaras: "available on both").
STT_MODEL = os.environ.get("VAANI_STT_MODEL", "saaras:v4")
STT_TRANSLATE_MODEL = os.environ.get("VAANI_STT_TRANSLATE_MODEL", "saaras:v4")
TRANSLATE_MODEL = os.environ.get("VAANI_TRANSLATE_MODEL", "mayura:v1")  # 1000 chars; sarvam-translate:v1 = 2000
TRANSLATE_CHAR_LIMIT = 1000 if TRANSLATE_MODEL.startswith("mayura") else 2000
TTS_MODEL = os.environ.get("VAANI_TTS_MODEL", "bulbul:v4-flash")
TTS_FALLBACK_MODEL = "bulbul:v3"  # used for langs with no v4-flash voice (ml-IN, od-IN)
TTS_SPEAKER = os.environ.get("VAANI_TTS_SPEAKER", "")  # empty -> per-language map below
TTS_SAMPLE_RATE = 24000
TTS_CHAR_LIMIT = 2500
# bulbul:v4-flash speakers are language-specific: <name>_<lang>_<style>. All IDs below verified verbatim
# in docs text-to-speech/voices (Oct 2026). v4-flash has no ml-IN / od-IN voices -> bulbul:v3 fallback.
TTS_SPEAKERS_V4 = {
    "hi-IN": "sanchita_hi_assistant",
    "en-IN": "roopa_en_conversational",
    "bn-IN": "roopa_bn_conversational",
    "ta-IN": "gokul_ta_narration",
    "te-IN": "pooja_te_conversation",
    "kn-IN": "chetan_kn_conversation",
    "mr-IN": "ishita_mr_conversational",
    "gu-IN": "pooja_gu_conversational",
    "pa-IN": "anand_pa_conversation",
}
TTS_SPEAKER_V3 = "shubh"  # bulbul:v3 speakers are multilingual
AGENT_MODEL = os.environ.get("VAANI_AGENT_MODEL", "sarvam-105b")
FALLBACK_AGENT_MODEL = os.environ.get("VAANI_FALLBACK_MODEL", "sarvam-105b")  # sarvam-m deprecated
VISION_MODEL = os.environ.get("VAANI_VISION_MODEL", "gemma4")
KNOWLEDGE_MODEL = os.environ.get("VAANI_KNOWLEDGE_MODEL", "sarvam-105b")
# None -> reasoning off (sent as JSON null; v1 docs: "Can be disabled by explicitly setting to None").
# v1 enum: "low"/"high"/"max" (guide also documents "medium", the default).
REASONING_EFFORT = os.environ.get("VAANI_REASONING_EFFORT") or None

TIMEOUT = 20
RETRIES = 1
MAX_AGENT_STEPS = 6

# --- Local behaviour ---
PTT_KEY = os.environ.get("VAANI_PTT_KEY", "f9")
WAKE_WORD = os.environ.get("VAANI_WAKE_WORD", "hey vaani")
WAKE_ENABLED = os.environ.get("VAANI_WAKE", "1") == "1"
SAMPLE_RATE = 16000
MIC_DEVICE = None  # input device index chosen in setup (set by onboarding.apply_settings)
MIN_CLIP_SEC = 0.4
MAX_CLIP_SEC = 15
SILENCE_SEC = 1.2  # stop recording after this much silence (wake-word mode)
SAFE_MODE = os.environ.get("VAANI_SAFE_MODE", "1") == "1"
CLOUD_GUARD = os.environ.get("VAANI_CLOUD_GUARD", "1") == "1"  # Sarvam AI risk review in guardrails
GUARD_TIMEOUT = 10

OS_NAME = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(platform.system(), platform.system())
IS_WSL = "microsoft" in platform.release().lower()

SUPPORTED_LANGS = ["hi-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN", "en-IN"]
DEFAULT_LANG = "en-IN"
