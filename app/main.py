"""
FastAPI server: web UI + JSON API for voice cloning, batch DOCX generation,
and quiz-question audio generation.
"""
import io
import json
import os
import re
import tempfile
import zipfile
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .tts_engine import VoiceEngine
from . import voice_store
from . import auth
from . import batch_docx
from . import question_docx
from . import question_audio

app = FastAPI(title="Voice Clone + Question Tool", version="1.2.0")

# Split pasted “many questions” text into separate TTS jobs (own line, exact match).
QUESTION_SPLIT_MARKER = "<<<QUESTION>>>"
MAX_BATCH_QUESTIONS = 500

_ALLOWED_FORMATS = {"wav", "mp3"}


def _normalize_format(fmt: str) -> str:
    f = (fmt or "wav").strip().lower().lstrip(".")
    if f not in _ALLOWED_FORMATS:
        raise HTTPException(400, f"Unsupported format '{fmt}'. Use wav or mp3.")
    return f


def _encode_audio(wav, sr, fmt: str) -> tuple[bytes, str]:
    """Returns (bytes, media_type) for the given format."""
    if fmt == "mp3":
        return VoiceEngine.to_mp3_bytes(wav, sr), "audio/mpeg"
    return VoiceEngine.to_wav_bytes(wav, sr), "audio/wav"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine: Optional[VoiceEngine] = None


@app.on_event("startup")
def _startup():
    global engine
    engine = VoiceEngine()


# ---------- static web UI ----------
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(WEB_DIR, "index.html"), "r") as f:
        return f.read()


@app.get("/style.css")
def _css():
    return Response(
        content=open(os.path.join(WEB_DIR, "style.css")).read(),
        media_type="text/css",
    )


@app.get("/app.js")
def _js():
    return Response(
        content=open(os.path.join(WEB_DIR, "app.js")).read(),
        media_type="application/javascript",
    )


@app.get("/batch_generate.js")
def _batch_js():
    return Response(
        content=open(os.path.join(WEB_DIR, "batch_generate.js")).read(),
        media_type="application/javascript",
    )


@app.get("/question_generate.js")
def _question_js():
    return Response(
        content=open(os.path.join(WEB_DIR, "question_generate.js")).read(),
        media_type="application/javascript",
    )


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------- auth status (public) ----------
@app.get("/api/auth/status")
def auth_status():
    """Tells the UI whether auth is required yet (bootstrap vs normal mode)."""
    return {"auth_required": auth.any_keys_exist()}


# ---------- API keys management (protected) ----------
@app.get("/api/keys", dependencies=[Depends(auth.require_auth)])
def api_list_keys():
    return {"keys": auth.list_keys_safe()}


@app.post("/api/keys", dependencies=[Depends(auth.require_auth)])
def api_create_key(name: str = Form(...)):
    try:
        entry = auth.create_key(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # return the full key once on creation
    return {
        "id": entry["id"],
        "name": entry["name"],
        "key": entry["key"],
        "created_at": entry["created_at"],
    }


@app.delete("/api/keys/{key_id}", dependencies=[Depends(auth.require_auth)])
def api_delete_key(key_id: str):
    if not auth.delete_key(key_id):
        raise HTTPException(404, "key not found")
    return {"deleted": key_id}


# ---------- Voices (protected) ----------
@app.get("/api/voices", dependencies=[Depends(auth.require_auth)])
def api_list_voices():
    return {"voices": voice_store.list_voices()}


@app.post("/api/voices", dependencies=[Depends(auth.require_auth)])
async def api_create_voice(
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    if not name.strip():
        raise HTTPException(400, "name required")

    suffix = os.path.splitext(file.filename or "upload.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        meta = voice_store.create_voice(name.strip(), description.strip(), tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Failed to process audio: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return meta


@app.delete("/api/voices/{voice_id}", dependencies=[Depends(auth.require_auth)])
def api_delete_voice(voice_id: str):
    if not voice_store.delete_voice(voice_id):
        raise HTTPException(404, "voice not found")
    return {"deleted": voice_id}


# ---------- Generate (protected) ----------
@app.post("/api/generate", dependencies=[Depends(auth.require_auth)])
async def api_generate(
    voice_id: str = Form(...),
    text: str = Form(...),
    speed: float = Form(1.0),
    format: str = Form("wav"),
):
    if engine is None:
        raise HTTPException(503, "engine not ready")
    ref = voice_store.get_reference_wav(voice_id)
    if not ref:
        raise HTTPException(404, "voice_id not found")
    if not text.strip():
        raise HTTPException(400, "text required")
    fmt = _normalize_format(format)
    try:
        wav, sr = engine.generate(text=text, speaker_wav=ref, speed=speed)
    except Exception as e:
        raise HTTPException(500, f"generation failed: {e}")
    try:
        data, media_type = _encode_audio(wav, sr, fmt)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{voice_id}.{fmt}"'},
    )


def _split_questions_blob(raw: str) -> List[str]:
    """Split user text on QUESTION_SPLIT_MARKER lines; trim and drop empties."""
    parts = re.split(
        rf"(?:^\s*{re.escape(QUESTION_SPLIT_MARKER)}\s*$)",
        raw,
        flags=re.MULTILINE,
    )
    out = [p.strip() for p in parts if p.strip()]
    return out


@app.post("/api/generate-batch", dependencies=[Depends(auth.require_auth)])
async def api_generate_batch(
    voice_id: str = Form(...),
    text: str = Form(...),
    speed: float = Form(1.0),
    format: str = Form("wav"),
):
    """
    One audio file per question, returned inside a single ZIP.
    Separate questions in `text` with a line containing only: <<<QUESTION>>>
    """
    if engine is None:
        raise HTTPException(503, "engine not ready")
    ref = voice_store.get_reference_wav(voice_id)
    if not ref:
        raise HTTPException(404, "voice_id not found")
    raw = (text or "").strip()
    if not raw:
        raise HTTPException(400, "text required")
    fmt = _normalize_format(format)

    questions = _split_questions_blob(raw)
    if not questions:
        raise HTTPException(400, "no question text after splitting")
    if len(questions) > MAX_BATCH_QUESTIONS:
        raise HTTPException(
            400,
            f"Too many questions ({len(questions)}). Maximum is {MAX_BATCH_QUESTIONS}.",
        )

    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i, q in enumerate(questions):
                try:
                    wav, sr = engine.generate(text=q, speaker_wav=ref, speed=speed)
                except Exception as e:
                    raise HTTPException(
                        500,
                        f"generation failed on question {i + 1} of {len(questions)}: {e}",
                    ) from e
                try:
                    payload, _ = _encode_audio(wav, sr, fmt)
                except RuntimeError as e:
                    raise HTTPException(500, str(e)) from e
                name = f"question_{i + 1:03d}.{fmt}"
                zf.writestr(name, payload)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"batch zip failed: {e}") from e

    data = buf.getvalue()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="questions.zip"',
        },
    )


@app.post("/api/batch/parse-docx", dependencies=[Depends(auth.require_auth)])
async def api_batch_parse_docx(
    file: UploadFile = File(...),
    audio_extension: str = Form("wav"),
):
    """
    Parse a .docx voice script (Format A: Voice N list; Format B: Animation groups).
    Returns JSON entries for client-side batch generation against /api/generate.
    `audio_extension` controls the extension stamped into relative_path / path_parts.
    """
    fname = (file.filename or "").lower()
    if not fname.endswith(".docx"):
        raise HTTPException(400, "Upload a .docx file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    ext = _normalize_format(audio_extension)
    try:
        return batch_docx.parse_voice_docx(data, audio_extension=ext)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read document: {e}")


# ---------- Quiz questions (protected) ----------
@app.post("/api/questions/parse-docx", dependencies=[Depends(auth.require_auth)])
async def api_questions_parse_docx(file: UploadFile = File(...)):
    """
    Parse a quiz-question .docx into structured questions with pre-built voice scripts.
    """
    fname = (file.filename or "").lower()
    if not fname.endswith(".docx"):
        raise HTTPException(400, "Upload a .docx file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    try:
        return question_docx.parse_question_docx(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read document: {e}")


@app.post("/api/questions/generate-one", dependencies=[Depends(auth.require_auth)])
async def api_questions_generate_one(
    voice_id: str = Form(...),
    question_json: str = Form(...),
    speed: float = Form(1.0),
    format: str = Form("wav"),
    pause_seconds: float = Form(question_audio.DEFAULT_PAUSE_SEC),
):
    """
    Synthesise a single question's four voices and return one merged audio clip.
    Per-voice TTS failures are caught and substituted with silence; only a *total*
    failure (every voice broken or zero output) returns an HTTP error.
    """
    import traceback as _tb

    if engine is None:
        raise HTTPException(503, "engine not ready")
    ref = voice_store.get_reference_wav(voice_id)
    if not ref:
        raise HTTPException(404, "voice_id not found")
    fmt = _normalize_format(format)
    try:
        question = json.loads(question_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "question_json must be valid JSON")
    if not isinstance(question, dict):
        raise HTTPException(400, "question_json must be a JSON object")
    qnum = question.get("number", "x")
    try:
        data, media_type, ext, failures = question_audio.generate_question_audio(
            engine,
            speaker_wav=ref,
            question=question,
            speed=speed,
            fmt=fmt,
            pause_sec=pause_seconds,
        )
    except ValueError as e:
        print(f"[/api/questions/generate-one] Q{qnum} ValueError: {e}")
        raise HTTPException(400, f"Q{qnum}: {e}")
    except RuntimeError as e:
        print(f"[/api/questions/generate-one] Q{qnum} RuntimeError: {e}")
        raise HTTPException(500, f"Q{qnum}: {e}")
    except Exception as e:
        print(f"[/api/questions/generate-one] Q{qnum} unexpected error:")
        _tb.print_exc()
        raise HTTPException(500, f"Q{qnum} generation failed: {e}")

    headers = {"Content-Disposition": f'attachment; filename="Question {qnum}.{ext}"'}
    if failures:
        # Comma-joined and ASCII-safe for HTTP header transport.
        joined = "; ".join(failures)
        safe = joined.encode("ascii", "replace").decode("ascii")
        headers["X-Question-Voice-Failures"] = safe[:900]
        print(f"[/api/questions/generate-one] Q{qnum} produced with degraded voices: {joined}")
    return Response(content=data, media_type=media_type, headers=headers)


# ---------- Health (public) ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "device": engine.device if engine else "loading",
        "voices": len(voice_store.list_voices()),
        "auth_required": auth.any_keys_exist(),
    }
