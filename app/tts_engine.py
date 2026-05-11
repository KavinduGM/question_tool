"""
XTTS v2 voice cloning + generation engine.
Loads the model once at startup, reuses for all requests.
"""
import os
import io
import re
import shutil
import subprocess
import torch
import numpy as np
import soundfile as sf
from typing import List
from TTS.api import TTS

# Accept Coqui license automatically (non-interactive server use)
os.environ.setdefault("COQUI_TOS_AGREED", "1")


# Marks "12. " list-style periods so sentence splitting does not break after the dot.
_LIST_PERIOD_MARK = "\uE000"

# SSML-style pause: <break time="1.5s" />, time="1500ms", or <break /> (uses default 1.5s)
_BREAK_TAG_RE = re.compile(
    r'(?is)<\s*break(?:\s+time\s*=\s*["\']?(\d+(?:\.\d+)?)\s*(ms|s)?\s*["\']?)?\s*/\s*>'
)

# Block whole-line / whole-word scripts that imply non-English input (Latin + common STEM Greek kept).
_NON_ENGLISH_SCRIPT_RE = re.compile(
    r"[\u0400-\u04FF"  # Cyrillic
    r"\u0590-\u05FF"  # Hebrew
    r"\u0600-\u06FF"  # Arabic
    r"\u0700-\u074F"  # Syriac
    r"\u0780-\u07BF"  # Thaana
    r"\u0900-\u097F"  # Devanagari
    r"\u0980-\u09FF"  # Bengali
    r"\u0A00-\u0AFF"  # Gurmukhi / Gujarati
    r"\u0B00-\u0BFF"  # Tamil / Telugu / etc.
    r"\u0C00-\u0CFF"  # Kannada / Malayalam
    r"\u0D00-\u0DFF"  # Sinhala
    r"\u0E00-\u0E7F"  # Thai
    r"\u0F00-\u0FFF"  # Tibetan
    r"\u3040-\u309F"  # Hiragana
    r"\u30A0-\u30FF"  # Katakana
    r"\u4E00-\u9FFF"  # CJK Unified Ideographs
    r"\uAC00-\uD7AF"  # Hangul syllables
    r"\u1100-\u11FF"  # Hangul Jamo
    r"]"
)


class VoiceEngine:
    """XTTS is multilingual; this app fixes synthesis to English only."""

    LANGUAGE = "en"
    DEFAULT_PAUSE_SEC = 1.5  # <break /> or omitting time= on a break tag
    INTERNAL_CHUNK_GAP_SEC = 0.25  # between automatic XTTS length chunks only
    MAX_BREAK_PAUSE_SEC = 30.0

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[tts_engine] Loading XTTS v2 on device: {self.device}")
        self.tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)
        print("[tts_engine] Model loaded (language fixed to English).")

    @staticmethod
    def _protect_list_periods(text: str) -> str:
        """
        Hide periods after digits when followed by whitespace so they are not treated
        as sentence ends (e.g. '2. __' must stay one phrase, not ['2.', '__']).
        Decimals like '3.14' are unchanged (no whitespace after the first dot).
        """
        return re.sub(
            r"(\d+)\.(\s)",
            lambda m: f"{m.group(1)}{_LIST_PERIOD_MARK}{m.group(2)}",
            text,
        )

    @staticmethod
    def _restore_list_periods(text: str) -> str:
        return text.replace(_LIST_PERIOD_MARK, ".")

    @staticmethod
    def _assert_english_only(text: str) -> None:
        """Reject text that contains non-Latin scripts (this app only supports English TTS)."""
        m = _NON_ENGLISH_SCRIPT_RE.search(text)
        if m:
            raise ValueError(
                "Only English text is supported. Remove non-Latin script characters "
                f"(for example near: {text[max(0, m.start() - 12) : m.start() + 12]!r})."
            )

    @staticmethod
    def _parse_ssml_breaks(text: str) -> tuple[List[str], List[float]]:
        """
        Split on <break … />: optional time (default DEFAULT_PAUSE_SEC). Returns (segments, pause_after_seconds).
        pause_after[i] is the silence after segments[i]; last entry is always 0.0.
        """
        segments: List[str] = []
        pauses: List[float] = []
        pos = 0
        for m in _BREAK_TAG_RE.finditer(text):
            segments.append(text[pos : m.start()].strip())
            if m.group(1) is None:
                sec = VoiceEngine.DEFAULT_PAUSE_SEC
            else:
                val = float(m.group(1))
                unit = (m.group(2) or "s").lower()
                sec = val / 1000.0 if unit == "ms" else val
            sec = max(0.0, min(sec, VoiceEngine.MAX_BREAK_PAUSE_SEC))
            pauses.append(sec)
            pos = m.end()
        segments.append(text[pos:].strip())
        pauses.append(0.0)
        return segments, pauses

    # ---------- text chunking ----------
    @staticmethod
    def _split_text(text: str, max_chars: int = 240) -> List[str]:
        """
        Split text into chunks that respect sentence boundaries.
        XTTS handles ~250 chars per inference cleanly; longer produces artifacts.
        """
        text = text.strip().replace("\r", "")
        if not text:
            return []

        text = VoiceEngine._protect_list_periods(text)
        # split into sentences (list markers like "2. " no longer end with '.' for this step)
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks: List[str] = []
        buf = ""
        for s in sentences:
            s = VoiceEngine._restore_list_periods(s).strip()
            if not s:
                continue
            # if sentence itself too long, hard-split on commas / spaces
            if len(s) > max_chars:
                parts = re.split(r'(?<=[,;:])\s+', s)
                for p in parts:
                    if len(p) > max_chars:
                        # Flush buf before word-splitting: otherwise chunks from this
                        # part get appended while buf still holds an earlier clause.
                        if buf:
                            chunks.append(buf.strip())
                            buf = ""
                        # last resort: break on spaces
                        words = p.split()
                        cur = ""
                        for w in words:
                            if len(cur) + len(w) + 1 > max_chars:
                                if cur:
                                    chunks.append(cur.strip())
                                cur = w
                            else:
                                cur = (cur + " " + w).strip()
                        if cur:
                            if buf and len(buf) + len(cur) + 1 <= max_chars:
                                buf = (buf + " " + cur).strip()
                            else:
                                if buf:
                                    chunks.append(buf.strip())
                                buf = cur
                    else:
                        if buf and len(buf) + len(p) + 1 <= max_chars:
                            buf = (buf + " " + p).strip()
                        else:
                            if buf:
                                chunks.append(buf.strip())
                            buf = p
                continue

            if buf and len(buf) + len(s) + 1 <= max_chars:
                buf = (buf + " " + s).strip()
            else:
                if buf:
                    chunks.append(buf.strip())
                buf = s
        if buf:
            chunks.append(buf.strip())
        return [VoiceEngine._restore_list_periods(c) for c in chunks]

    # ---------- generation ----------
    def generate(
        self,
        text: str,
        speaker_wav,
        speed: float = 1.0,
    ) -> tuple[np.ndarray, int]:
        """
        Generate speech audio from text using the given reference wav.
        `speaker_wav` may be a single path *or a list of paths* — Coqui XTTS averages
        the speaker embedding across multiple clips, which is the main lever for
        boosting cloning fidelity with extra reference material.
        English only. Use <break /> or <break time="…" /> between sections (default pause 1.5s).
        Returns (float32 waveform mono, sample_rate).
        """
        speed = float(max(0.5, min(2.0, speed)))
        raw = (text or "").strip()
        if not raw:
            raise ValueError("Empty text")

        VoiceEngine._assert_english_only(_BREAK_TAG_RE.sub("", raw))

        segments, pause_after = VoiceEngine._parse_ssml_breaks(raw)
        if not any(seg.strip() for seg in segments):
            raise ValueError("Empty text")

        sample_rate = 24000  # XTTS v2 native
        audio_parts: List[np.ndarray] = []
        gap_short = np.zeros(
            int(self.INTERNAL_CHUNK_GAP_SEC * sample_rate), dtype=np.float32
        )

        for seg_idx, segment in enumerate(segments):
            segment = segment.strip()
            if not segment:
                if pause_after[seg_idx] > 0:
                    audio_parts.append(
                        np.zeros(int(pause_after[seg_idx] * sample_rate), dtype=np.float32)
                    )
                continue

            chunks = self._split_text(segment)
            if not chunks:
                if pause_after[seg_idx] > 0:
                    audio_parts.append(
                        np.zeros(int(pause_after[seg_idx] * sample_rate), dtype=np.float32)
                    )
                continue

            for i, chunk in enumerate(chunks):
                print(
                    f"[tts_engine] segment {seg_idx + 1}/{len(segments)} "
                    f"chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)"
                )
                wav = self.tts.tts(
                    text=chunk,
                    speaker_wav=speaker_wav,
                    language=self.LANGUAGE,
                    speed=speed,
                )
                wav = np.array(wav, dtype=np.float32)
                audio_parts.append(wav)
                if i < len(chunks) - 1:
                    audio_parts.append(gap_short)

            if pause_after[seg_idx] > 0:
                audio_parts.append(
                    np.zeros(int(pause_after[seg_idx] * sample_rate), dtype=np.float32)
                )

        full = np.concatenate(audio_parts) if audio_parts else np.zeros(0, dtype=np.float32)
        # normalize lightly to prevent clipping
        peak = np.max(np.abs(full)) if full.size else 1.0
        if peak > 0.99:
            full = full * (0.99 / peak)
        return full, sample_rate

    @staticmethod
    def to_wav_bytes(wav: np.ndarray, sample_rate: int) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, wav, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    @staticmethod
    def to_mp3_bytes(wav: np.ndarray, sample_rate: int, bitrate: str = "192k") -> bytes:
        """Encode the waveform as MP3 by piping WAV through ffmpeg + libmp3lame."""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required for MP3 export but was not found on PATH.")
        wav_bytes = VoiceEngine.to_wav_bytes(wav, sample_rate)
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "wav", "-i", "pipe:0",
                "-codec:a", "libmp3lame", "-b:a", bitrate,
                "-f", "mp3", "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg MP3 encode failed: {proc.stderr.decode('utf-8', 'ignore')}")
        return proc.stdout
