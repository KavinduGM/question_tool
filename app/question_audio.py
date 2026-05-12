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

# How Voice 1 (the intro) is built:
#   "full"   → "Question <number-spelled>. <question text>"  (default; filename "Question N.<ext>")
#   "number" → "<number-spelled>."                            (no question body; filename "N.<ext>")
INTRO_STYLES = ("full", "number")
DEFAULT_INTRO_STYLE = "full"

log = logging.getLogger(__name__)


def _silence(seconds: float, sample_rate: int) -> np.ndarray:
    seconds = max(MIN_PAUSE_SEC, min(MAX_PAUSE_SEC, float(seconds)))
    n = int(round(seconds * sample_rate))
    return np.zeros(n, dtype=np.float32)


# ---- number → words (English, up to 999,999) ----------------------------------
_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _under_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] if o == 0 else f"{_TENS[t]}-{_ONES[o]}"


def _under_thousand(n: int) -> str:
    if n < 100:
        return _under_hundred(n)
    h, r = divmod(n, 100)
    return f"{_ONES[h]} hundred" if r == 0 else f"{_ONES[h]} hundred {_under_hundred(r)}"


def number_to_words(n) -> str:
    """Spell a non-negative integer 0–999,999 in English. Falls back to str(n)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 0 or n >= 1_000_000:
        return str(n)
    if n < 1000:
        return _under_thousand(n)
    thousands, rest = divmod(n, 1000)
    head = f"{_under_thousand(thousands)} thousand"
    return head if rest == 0 else f"{head} {_under_thousand(rest)}"


def _voice1_for_style(question: dict, intro_style: str) -> str:
    number = question.get("number", "?")
    body = (question.get("question_text") or "").strip()
    spoken_number = number_to_words(number) if isinstance(number, int) or (
        isinstance(number, str) and number.isdigit()
    ) else str(number)
    if intro_style == "number":
        # Just the number on its own, no "Question" prefix and no body. XTTS
        # behaves much better on natural English than on bare digits, so we
        # always pronounce it as words.
        return f"{spoken_number}."
    # "full" — the default
    if body:
        return f"Question {spoken_number}. {body}"
    return f"Question {spoken_number}."


def voice_texts(question: dict, intro_style: str = DEFAULT_INTRO_STYLE) -> List[str]:
    """Return the four voice scripts in order for a parsed question dict."""
    vt = question.get("voice_texts") or {}
    return [
        _voice1_for_style(question, intro_style),
        (vt.get("voice2") or "").strip(),
        (vt.get("voice3") or "").strip(),
        (vt.get("voice4") or "").strip(),
    ]


def _render_one(
    engine: VoiceEngine,
    speaker_wav,
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
    speaker_wav,
    question: dict,
    speed: float,
    fmt: str,
    pause_sec: float = DEFAULT_PAUSE_SEC,
    intro_style: str = DEFAULT_INTRO_STYLE,
) -> Tuple[bytes, str, str, List[str]]:
    """
    Synthesise the four parts in order, concatenate with `pause_sec` of silence between
    each non-empty pair, and return (encoded_bytes, media_type, file_extension, failures).
    `failures` lists per-voice errors (empty list when everything succeeded).
    `intro_style` controls Voice 1: "full" → "Question N. <body>"; "number" → "N.".
    """
    if intro_style not in INTRO_STYLES:
        intro_style = DEFAULT_INTRO_STYLE
    parts = voice_texts(question, intro_style=intro_style)
    qnum = question.get("number", "?")
    rendered: List[np.ndarray] = []
    sr: int = 0
    failures: List[str] = []

    for idx, txt in enumerate(parts):
        wav, sr_part = _render_one(engine, speaker_wav, txt, speed, qnum, idx, failures)
        if sr == 0 and sr_part:
            sr = sr_part
        # Defence in depth: even though VoiceEngine._trim_trailing_noise already runs
        # per-chunk inside the engine, peak normalisation in engine.generate scales
        # the whole waveform and may revive tiny tail samples above the noise floor.
        # Trim the assembled voice one more time so the gap between this voice and
        # the next (pause_sec of zeros) is dead silent.
        if wav.size > 0 and sr_part:
            wav = VoiceEngine._trim_trailing_noise(wav, sr_part)
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
