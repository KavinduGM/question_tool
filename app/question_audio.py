"""
Generate one merged audio clip per quiz question by synthesising the four voice parts
(Voice 1: question, Voice 2: answers + correct answer, Voice 3: correct explanation,
Voice 4: incorrect explanations) and concatenating them with a configurable gap.

Per-voice failures (XTTS hiccup, weird text, etc.) are caught and substituted with
silence so a single bad chunk does not lose the entire question.
"""
from __future__ import annotations

import logging
import traceback
from typing import List, Tuple

import numpy as np

from .tts_engine import VoiceEngine

DEFAULT_PAUSE_SEC = 1.5
MIN_PAUSE_SEC = 0.0
MAX_PAUSE_SEC = 10.0

# Minimum number of samples in the final audio before we consider it a real file
# (≈ 0.5 seconds at 24 kHz). Below this we refuse to encode and raise — better to
# fail loudly than write a 44-byte WAV stub Windows can't play.
_MIN_FINAL_SAMPLES = 12_000

log = logging.getLogger(__name__)


def _silence(seconds: float, sample_rate: int) -> np.ndarray:
    seconds = max(MIN_PAUSE_SEC, min(MAX_PAUSE_SEC, float(seconds)))
    n = int(round(seconds * sample_rate))
    return np.zeros(n, dtype=np.float32)


def voice_texts(question: dict) -> List[str]:
    """Return the four voice scripts in order for a parsed question dict."""
    vt = question.get("voice_texts") or {}
    return [
        (vt.get("voice1") or "").strip(),
        (vt.get("voice2") or "").strip(),
        (vt.get("voice3") or "").strip(),
        (vt.get("voice4") or "").strip(),
    ]


def _render_one(
    engine: VoiceEngine,
    speaker_wav: str,
    text: str,
    speed: float,
    qnum,
    voice_idx: int,
    failures: List[str],
    retries: int = 1,
) -> Tuple[np.ndarray, int]:
    """
    Try to synthesise one voice. On failure (after `retries` retry attempts), log the
    error, record it in `failures`, and return an empty array so the rest of the
    question can still be produced.
    """
    if not text:
        return np.zeros(0, dtype=np.float32), 0
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            wav, sr_part = engine.generate(text=text, speaker_wav=speaker_wav, speed=speed)
            wav = np.asarray(wav, dtype=np.float32)
            # Scrub NaN / Inf — XTTS occasionally emits non-finite samples for awkward
            # inputs, and PCM_16 conversion of those produces unplayable WAVs on Windows.
            if not np.all(np.isfinite(wav)):
                wav = np.nan_to_num(wav, nan=0.0, posinf=0.99, neginf=-0.99)
            return wav, sr_part
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning(
                "[question_audio] Q%s voice %d attempt %d/%d failed: %s",
                qnum, voice_idx + 1, attempt + 1, retries + 1, e,
            )
    # All attempts failed.
    log.error(
        "[question_audio] Q%s voice %d GAVE UP after %d attempts. Substituting silence.",
        qnum, voice_idx + 1, retries + 1,
    )
    traceback.print_exception(type(last_err), last_err, last_err.__traceback__ if last_err else None)
    failures.append(f"voice {voice_idx + 1}: {last_err}")
    return np.zeros(0, dtype=np.float32), 0


def generate_question_audio(
    engine: VoiceEngine,
    speaker_wav: str,
    question: dict,
    speed: float,
    fmt: str,
    pause_sec: float = DEFAULT_PAUSE_SEC,
) -> Tuple[bytes, str, str, List[str]]:
    """
    Synthesise the four parts in order, concatenate with `pause_sec` of silence between
    each non-empty pair, and return (encoded_bytes, media_type, file_extension, failures).
    `failures` lists per-voice errors (empty list when everything succeeded).
    """
    parts = voice_texts(question)
    qnum = question.get("number", "?")
    rendered: List[np.ndarray] = []
    sr: int = 0
    failures: List[str] = []

    for idx, txt in enumerate(parts):
        wav, sr_part = _render_one(engine, speaker_wav, txt, speed, qnum, idx, failures)
        if sr == 0 and sr_part:
            sr = sr_part
        rendered.append(wav)

    if sr == 0:
        sr = 24000

    gap = _silence(pause_sec, sr)
    pieces: List[np.ndarray] = []
    placed = 0
    for arr in rendered:
        if arr.size == 0:
            continue
        if placed > 0 and gap.size > 0:
            pieces.append(gap)
        pieces.append(arr)
        placed += 1

    if not pieces:
        # Every voice failed or every voice was empty. Refuse to emit a 0-byte stub —
        # the caller should see a clear error instead of a "corrupt WAV" on disk.
        raise RuntimeError(
            f"Q{qnum}: no audio was produced (all four voices empty or failed). "
            f"Details: {'; '.join(failures) if failures else 'no voice text available'}"
        )

    final = np.concatenate(pieces)
    # Final safety pass: clip + scrub non-finite values.
    if not np.all(np.isfinite(final)):
        final = np.nan_to_num(final, nan=0.0, posinf=0.99, neginf=-0.99)
    np.clip(final, -1.0, 1.0, out=final)

    if final.size < _MIN_FINAL_SAMPLES:
        raise RuntimeError(
            f"Q{qnum}: produced audio too short ({final.size} samples). "
            f"Details: {'; '.join(failures) if failures else 'unknown'}"
        )

    # Light peak normalisation (mirrors VoiceEngine.generate).
    peak = float(np.max(np.abs(final)))
    if peak > 0.99:
        final = final * (0.99 / peak)

    fmt = (fmt or "wav").strip().lower()
    if fmt == "mp3":
        return VoiceEngine.to_mp3_bytes(final, sr), "audio/mpeg", "mp3", failures
    return VoiceEngine.to_wav_bytes(final, sr), "audio/wav", "wav", failures
