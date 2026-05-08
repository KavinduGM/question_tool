"""
Generate one merged audio clip per quiz question by synthesising the four voice parts
(Voice 1: question, Voice 2: answers + correct answer, Voice 3: correct explanation,
Voice 4: incorrect explanations) and concatenating them with a configurable gap.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

from .tts_engine import VoiceEngine

DEFAULT_PAUSE_SEC = 1.5
MIN_PAUSE_SEC = 0.0
MAX_PAUSE_SEC = 10.0


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


def generate_question_audio(
    engine: VoiceEngine,
    speaker_wav: str,
    question: dict,
    speed: float,
    fmt: str,
    pause_sec: float = DEFAULT_PAUSE_SEC,
) -> Tuple[bytes, str, str]:
    """
    Synthesise the four parts in order, concatenate with `pause_sec` of silence between
    each pair, and return (encoded_bytes, media_type, file_extension).
    Empty voice parts are skipped (no audio, no silence).
    """
    parts = voice_texts(question)
    rendered: List[np.ndarray] = []
    sr: int = 0

    for txt in parts:
        if not txt:
            rendered.append(np.zeros(0, dtype=np.float32))
            continue
        wav, sr_part = engine.generate(text=txt, speaker_wav=speaker_wav, speed=speed)
        if sr == 0:
            sr = sr_part
        rendered.append(wav.astype(np.float32, copy=False))

    if sr == 0:
        # All four parts were empty — produce a tiny silent file rather than crashing.
        sr = 24000
        rendered = [np.zeros(int(0.05 * sr), dtype=np.float32)]

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
        pieces = [np.zeros(int(0.05 * sr), dtype=np.float32)]
    final = np.concatenate(pieces)

    # Light peak normalisation, mirroring VoiceEngine.generate.
    peak = float(np.max(np.abs(final))) if final.size else 1.0
    if peak > 0.99:
        final = final * (0.99 / peak)

    fmt = (fmt or "wav").strip().lower()
    if fmt == "mp3":
        return VoiceEngine.to_mp3_bytes(final, sr), "audio/mpeg", "mp3"
    return VoiceEngine.to_wav_bytes(final, sr), "audio/wav", "wav"
