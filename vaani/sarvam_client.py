"""Thin Sarvam AI REST client (STT, translate, TTS, chat, vision). Uses `requests` only."""
import base64
import io
import os
import re
import time
import wave

import requests

from . import config


class SarvamError(Exception):
    def __init__(self, msg, status=None, tools_unsupported=False):
        super().__init__(msg)
        self.status = status
        self.tools_unsupported = tools_unsupported


# ---------------------------------------------------------------- helpers
def _key():
    k = os.environ.get("SARVAM_API_KEY") or config.SARVAM_API_KEY
    if not k:
        raise SarvamError("SARVAM_API_KEY not set; run `vaani setup`")
    return k


def _headers(json_body=True):
    k = _key()
    h = {"api-subscription-key": k, "Authorization": f"Bearer {k}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _post(url, *, json=None, data=None, files_factory=None, timeout=None):
    """POST with one retry on network errors / 5xx. Returns parsed JSON."""
    timeout = timeout or config.TIMEOUT
    attempts = 1 + max(0, int(getattr(config, "RETRIES", 1)))
    last = None
    for i in range(attempts):
        try:
            files = files_factory() if files_factory else None
            try:
                r = requests.post(url, headers=_headers(json_body=files is None),
                                  json=json, data=data, files=files, timeout=timeout)
            finally:
                if files:
                    for v in files.values():
                        f = v[1] if isinstance(v, tuple) else v
                        if hasattr(f, "close"):
                            f.close()
        except requests.RequestException as e:
            last = SarvamError(f"Network error calling {url}: {e}")
            if i + 1 < attempts:
                time.sleep(0.6)
                continue
            raise last
        if r.status_code >= 500 and i + 1 < attempts:
            last = SarvamError(f"HTTP {r.status_code} from {url}: {r.text[:300]}", r.status_code)
            time.sleep(0.6)
            continue
        if r.status_code >= 400:
            raise SarvamError(f"HTTP {r.status_code} from {url}: {r.text[:500]}", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise SarvamError(f"Non-JSON response from {url}: {r.text[:300]}", r.status_code)
    raise last or SarvamError(f"Request to {url} failed")


def _norm_lang(code):
    if not code:
        return config.DEFAULT_LANG
    code = str(code).strip()
    if code in config.SUPPORTED_LANGS:
        return code
    # tolerate "hi" / "or-IN" style codes
    base = code.split("-")[0].lower()
    if base == "or":
        base = "od"
    cand = f"{base}-IN"
    return cand if cand in config.SUPPORTED_LANGS else config.DEFAULT_LANG


def _chunk(text, limit):
    """Split text into <=limit chunks, preferring sentence then word boundaries."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?।॥\n])\s+", text)
    chunks, cur = [], ""
    for p in parts:
        while len(p) > limit:  # very long sentence: split on whitespace / hard cut
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            piece, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if not p:
            continue
        if cur and len(cur) + 1 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _wav_file(wav_path):
    def factory():
        return {"file": (os.path.basename(str(wav_path)), open(wav_path, "rb"), "audio/wav")}
    return factory


# ---------------------------------------------------------------- STT
def _stt(wav_path, mode, language_code=None):
    data = {"model": config.STT_TRANSLATE_MODEL if mode == "translate" else config.STT_MODEL,
            "mode": mode}
    if language_code:
        data["language_code"] = language_code
    return _post(config.STT_URL, data=data, files_factory=_wav_file(wav_path))


def stt_translate(wav_path) -> dict:
    """Speech -> English text + detected language. {"text_en","lang","raw_transcript"}."""
    res = _stt(wav_path, "translate")
    text_en = (res.get("transcript") or "").strip()
    lang_raw = res.get("language_code")
    if lang_raw:
        return {"text_en": text_en, "lang": _norm_lang(lang_raw), "raw_transcript": text_en}
    # Fallback: native transcription with auto-detect, then text translate to English.
    res2 = _stt(wav_path, "transcribe", language_code="unknown")
    native = (res2.get("transcript") or "").strip()
    lang = _norm_lang(res2.get("language_code"))
    if lang != "en-IN" and native:
        try:
            text_en = translate(native, lang, "en-IN") or text_en
        except SarvamError:
            text_en = text_en or native
    elif not text_en:
        text_en = native
    return {"text_en": text_en, "lang": lang, "raw_transcript": native or text_en}


# ---------------------------------------------------------------- translate
def translate(text, src, tgt) -> str:
    if not text or not text.strip():
        return text or ""
    if src == tgt:
        return text
    limit = getattr(config, "TRANSLATE_CHAR_LIMIT", 1000)
    out = []
    for chunk in _chunk(text, limit):
        res = _post(config.TRANSLATE_URL, json={
            "input": chunk,
            "source_language_code": src or "auto",
            "target_language_code": tgt,
            "model": config.TRANSLATE_MODEL,
        })
        out.append(res.get("translated_text", ""))
    return " ".join(o for o in out if o)


# ---------------------------------------------------------------- TTS
def _voice_for(lang):
    lang = _norm_lang(lang)
    model = config.TTS_MODEL
    if config.TTS_SPEAKER:
        return model, config.TTS_SPEAKER, lang
    if model.startswith("bulbul:v4"):
        spk = config.TTS_SPEAKERS_V4.get(lang)
        if spk:
            return model, spk, lang
        return config.TTS_FALLBACK_MODEL, config.TTS_SPEAKER_V3, lang  # ml-IN / od-IN
    if model.startswith("bulbul:v3"):
        return model, config.TTS_SPEAKER_V3, lang
    return model, "anushka", lang  # bulbul:v2


def _concat_wavs(blobs):
    if len(blobs) == 1:
        return blobs[0]
    params, frames = None, []
    for b in blobs:
        try:
            with wave.open(io.BytesIO(b), "rb") as w:
                if params is None:
                    params = w.getparams()
                frames.append(w.readframes(w.getnframes()))
        except wave.Error:
            continue
    if params is None:
        return blobs[0]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        for f in frames:
            out.writeframes(f)
    return buf.getvalue()


def tts(text, lang_code) -> bytes:
    """Text -> playable WAV bytes."""
    model, speaker, lang = _voice_for(lang_code)
    blobs = []
    for chunk in _chunk(text, getattr(config, "TTS_CHAR_LIMIT", 2500)):
        res = _post(config.TTS_URL, json={
            "text": chunk,
            "language_code": lang,  # docs: required field is `language_code` (no target_language_code)
            "model": model,
            "speaker": speaker,
            "speech_sample_rate": getattr(config, "TTS_SAMPLE_RATE", 24000),
            "output_audio_codec": "wav",
        })
        for a in res.get("audios") or []:
            blobs.append(base64.b64decode(a))
    if not blobs:
        raise SarvamError("TTS returned no audio")
    return _concat_wavs(blobs)


# ---------------------------------------------------------------- chat
def chat(messages, tools=None, model=None, url=None) -> dict:
    body = {
        "model": model or config.AGENT_MODEL,
        "messages": messages,
        "reasoning_effort": getattr(config, "REASONING_EFFORT", None),  # None -> JSON null = off
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    try:
        return _post(url or config.CHAT_URL, json=body, timeout=max(config.TIMEOUT, 30))
    except SarvamError as e:
        if tools and e.status and 400 <= e.status < 500 and "tool" in str(e).lower():
            e.tools_unsupported = True
        raise


# ---------------------------------------------------------------- vision
def _image_data_uri(image_path, max_w=1280):
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, int(im.height * max_w / im.width)))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            data = buf.getvalue()
    except ImportError:
        with open(image_path, "rb") as f:
            data = f.read()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def vision_read(image_path, prompt) -> str:
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _image_data_uri(image_path)}},
    ]}]
    body = {"model": config.VISION_MODEL, "messages": messages}
    # gemma4 is served only on /v2/chat/completions (beta; key needs access). V1 accepts only
    # sarvam-105b* (text-only), so a v1 fallback cannot work.
    try:
        res = _post(getattr(config, "CHAT_V2_URL", config.CHAT_URL), json=body,
                    timeout=max(config.TIMEOUT, 45))
    except SarvamError as e:
        if e.status in (401, 403, 404):
            raise SarvamError(f"Vision ({config.VISION_MODEL}) unavailable - key may lack beta access "
                              f"to /v2/chat/completions. {e}", e.status)
        raise
    try:
        return (res["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        raise SarvamError(f"Unexpected vision response: {str(res)[:300]}")


# ---------------------------------------------------------------- transliterate / LID / knowledge
def transliterate(text, src, tgt) -> str:
    if not text or not text.strip():
        return text or ""
    res = _post(config.TRANSLITERATE_URL, json={
        "input": text[:1000], "source_language_code": src or "auto",
        "target_language_code": tgt}, timeout=config.TIMEOUT)
    return res.get("transliterated_text", "")


def detect_language(text) -> dict:
    res = _post(config.TEXT_LID_URL, json={"input": (text or "")[:1000]}, timeout=config.TIMEOUT)
    return {"language_code": res.get("language_code"), "script_code": res.get("script_code")}


def ask(question) -> str:
    body = {"model": config.KNOWLEDGE_MODEL, "reasoning_effort": None, "messages": [
        {"role": "system", "content": "Answer factual/explanatory questions accurately in at most 3 short sentences. No markdown."},
        {"role": "user", "content": question}]}
    res = _post(config.CHAT_URL, json=body, timeout=max(config.TIMEOUT, 30))
    try:
        return (res["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        raise SarvamError(f"Unexpected chat response: {str(res)[:300]}")


# ---------------------------------------------------------------- doc-ai (Sarvam Vision digitise)
DOC_AI_BASE = getattr(config, "DOC_AI_BASE", "https://api.sarvam.ai/doc-ai/v1")
_DOC_MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_DOC_TERMINAL = {"completed", "partially_completed", "failed", "cancelled", "canceled", "error"}


def _doc_headers():
    # doc-ai rejects the extra Bearer header; subscription key only
    return {"api-subscription-key": _key()}


def digitise_document(path, lang="en-IN", output_format="md", max_wait=90, max_chars=6000) -> str:
    """Sarvam Vision doc-ai digitise: upload -> poll status -> join page block text."""
    ext = os.path.splitext(path)[1].lower()
    mime = _DOC_MIME.get(ext, "application/octet-stream")
    with open(path, "rb") as fh:
        r = requests.post(f"{DOC_AI_BASE}/job/digitise", headers=_doc_headers(),
                          data={"language": _norm_lang(lang) or "en-IN", "output_format": output_format,
                                "content_type": "printed", "auto_orient": "true"},
                          files={"file": (os.path.basename(path), fh, mime)},
                          timeout=max(config.TIMEOUT, 60))
    if r.status_code >= 400:
        raise SarvamError(f"doc-ai digitise HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    res = r.json()
    job = res.get("job_id")
    if not job:
        raise SarvamError(f"doc-ai: no job_id: {str(res)[:300]}")
    deadline = time.time() + max_wait
    status = res.get("status", "pending")
    while status not in _DOC_TERMINAL:
        if time.time() > deadline:
            raise SarvamError(f"doc-ai job {job} still '{status}' after {max_wait}s")
        time.sleep(2)
        r = requests.get(f"{DOC_AI_BASE}/job/{job}/status", headers=_doc_headers(), timeout=30)
        if r.status_code >= 400:
            raise SarvamError(f"doc-ai status HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        status = (r.json().get("status") or "").lower()
    if status not in ("completed", "partially_completed"):
        raise SarvamError(f"doc-ai job {job} ended with status '{status}'")
    r = requests.get(f"{DOC_AI_BASE}/job/{job}/results", headers=_doc_headers(), timeout=60)
    if r.status_code >= 400:
        raise SarvamError(f"doc-ai results HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    parts = []
    for doc in r.json().get("documents") or []:
        for page in doc.get("pages") or []:
            blocks = sorted(page.get("blocks") or [], key=lambda b: b.get("reading_order") or 0)
            txt = "\n".join((b.get("text") or "").strip() for b in blocks if (b.get("text") or "").strip())
            if txt:
                parts.append(f"[page {page.get('page_num')}]\n{txt}")
    out = "\n\n".join(parts).strip()
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n...[truncated, {len(out)} chars total]"
    return out


def answer_from_text(text, question) -> str:
    body = {"model": config.KNOWLEDGE_MODEL, "reasoning_effort": None, "messages": [
        {"role": "system", "content": "Answer the question using only the document text provided. Be brief (max 3 sentences). No markdown. If the answer is not in the document, say so."},
        {"role": "user", "content": f"DOCUMENT:\n{text}\n\nQUESTION: {question}"}]}
    res = _post(config.CHAT_URL, json=body, timeout=max(config.TIMEOUT, 45))
    try:
        return (res["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        raise SarvamError(f"Unexpected chat response: {str(res)[:300]}")


# ---------------------------------------------------------------- ping
def ping():
    try:
        out = translate("hello", "en-IN", "hi-IN")
        return True, f"OK (hello -> {out})"
    except SarvamError as e:
        return False, str(e)
