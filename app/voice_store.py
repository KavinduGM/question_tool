"""
Voice library: stores reference wavs + metadata on disk.
Each voice lives in voices/{voice_id}/ with:
  - reference.wav: the cleaned full-length reference (up to MAX_REF_SECONDS)
  - segment_000.wav, segment_001.wav, …: ~SEGMENT_SECONDS-long slices of reference.wav
    that XTTS uses as a *list* of conditioning clips (averaged speaker embedding,
    which improves cloning fidelity).
  - meta.json
"""
import glob
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

VOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "voices")
os.makedirs(VOICES_DIR, exist_ok=True)

# How much of the user's upload we keep after silence-trim. XTTS only conditions on
# ~6s internally but averaging across multiple clean clips noticeably helps fidelity.
MAX_REF_SECONDS = 900  # 15 minutes

# Segment length targets when cutting on silence boundaries.
SEGMENT_TARGET_SEC = 30
SEGMENT_MIN_SEC = 15
SEGMENT_MAX_SEC = 60

# Don't bother splitting if the cleaned reference is shorter than this.
SEGMENT_MIN_SOURCE_SECONDS = 45

# Cap on how many segments we actually keep for embedding averaging. Past ~12 the
# average dilutes toward "your speech in general" rather than "your specific voice
# in this recording", and inference also slows down linearly with segment count.
MAX_SEGMENTS_KEPT = 12

# XTTS-friendly loudness target. ffmpeg loudnorm normalises to -16, but the model
# was trained on material that's a touch quieter; -18 LUFS is the sweet spot for
# *quality scoring* of individual segments.
TARGET_LUFS = -18.0


def _voice_path(voice_id: str) -> str:
    return os.path.join(VOICES_DIR, voice_id)


def _run_ffmpeg(cmd: List[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-600:]}")


def _probe_duration_seconds(path: str) -> float:
    """Return duration in seconds via ffprobe, 0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def _prepare_reference(src_path: str, dst_path: str) -> None:
    """
    Convert input audio to a clean 24 kHz mono WAV for XTTS cloning.

    Filter chain:
      - mono / 24 kHz
      - high-pass at 60 Hz (removes room rumble + AC hum below 60 Hz)
      - silenceremove: collapse runs of silence longer than 0.4s to 0.4s.
        Long dead air poisons the speaker embedding and makes cloned voices
        sound flat / hesitant.
      - loudnorm: EBU R128 (-16 LUFS) so reference loudness matches XTTS
        training distribution.
      - capped at MAX_REF_SECONDS so a 30-minute upload doesn't blow up
        memory or split into 60 segments.

    Requires ffmpeg.
    """
    af = (
        "highpass=f=60,"
        "silenceremove="
        "start_periods=1:start_silence=0.1:start_threshold=-40dB:"
        "stop_periods=-1:stop_silence=0.4:stop_threshold=-40dB:"
        "detection=peak,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-ac", "1",
        "-ar", "24000",
        "-t", str(MAX_REF_SECONDS),
        "-af", af,
        dst_path,
    ]
    _run_ffmpeg(cmd)


def _silence_intervals(
    path: str,
    noise_db: float = -35.0,
    min_silence_sec: float = 0.25,
) -> List[Tuple[float, float]]:
    """
    Run ffmpeg silencedetect on `path` and return a list of (start_sec, end_sec)
    silence intervals. Used so we can split the reference on natural pauses
    instead of arbitrary 30-second boundaries.
    """
    cmd = [
        "ffmpeg", "-hide_banner",
        "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for line in res.stderr.splitlines():
        m1 = re.search(r"silence_start:\s+([\d.]+)", line)
        m2 = re.search(r"silence_end:\s+([\d.]+)", line)
        if m1:
            start = float(m1.group(1))
        elif m2 and start is not None:
            end = float(m2.group(1))
            if end > start:
                out.append((start, end))
            start = None
    return out


def _pack_speech_into_segments(
    speech: List[Tuple[float, float]],
    target: float = SEGMENT_TARGET_SEC,
    min_s: float = SEGMENT_MIN_SEC,
    max_s: float = SEGMENT_MAX_SEC,
) -> List[Tuple[float, float]]:
    """
    Greedy: walk speech intervals and merge consecutive ones into segments that
    are ≥ target seconds, never longer than max_s. Cuts ideally land on silence
    boundaries (the gaps between `speech` entries *are* silences). Any speech
    interval that's itself longer than max_s is pre-fragmented into ~target
    chunks so we never emit a giant segment when the audio has no natural pauses.
    Drops segments shorter than min_s.
    """
    fragmented: List[Tuple[float, float]] = []
    for s, e in speech:
        if (e - s) <= max_s:
            fragmented.append((s, e))
            continue
        cursor = s
        while cursor < e:
            end = min(cursor + target, e)
            fragmented.append((cursor, end))
            cursor = end

    segments: List[Tuple[float, float]] = []
    cur_start: Optional[float] = None
    cur_end: Optional[float] = None
    for sp_s, sp_e in fragmented:
        if cur_start is None:
            cur_start, cur_end = sp_s, sp_e
            continue
        if (sp_e - cur_start) > max_s:
            segments.append((cur_start, cur_end))
            cur_start, cur_end = sp_s, sp_e
            continue
        cur_end = sp_e
        if (cur_end - cur_start) >= target:
            segments.append((cur_start, cur_end))
            cur_start = cur_end = None
    if cur_start is not None and cur_end is not None:
        segments.append((cur_start, cur_end))
    return [(s, e) for s, e in segments if (e - s) >= min_s]


def _segment_lufs(path: str) -> Optional[float]:
    """Integrated loudness (LUFS) of a clip via ffmpeg's ebur128 filter."""
    cmd = [
        "ffmpeg", "-hide_banner",
        "-i", path,
        "-af", "ebur128=peak=true",
        "-f", "null", "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    last = None
    for line in res.stderr.splitlines():
        m = re.search(r"I:\s+([\-\d.]+)\s*LUFS", line)
        if m:
            try:
                last = float(m.group(1))
            except ValueError:
                pass
    return last


def _rank_and_trim_segments(seg_paths: List[str], max_kept: int = MAX_SEGMENTS_KEPT) -> List[str]:
    """
    Score each segment by closeness to TARGET_LUFS, keep top `max_kept` in their
    original chronological order, delete the rest from disk.

    Why: averaging across too many segments dilutes the speaker embedding
    toward a generic profile. Coqui community guidance is ~5-10 clean clips.
    """
    if not seg_paths:
        return []
    # Drop a too-short trailing segment (silence-aligned cuts can leave a stub).
    if len(seg_paths) > 1:
        tail_dur = _probe_duration_seconds(seg_paths[-1])
        if tail_dur < SEGMENT_MIN_SEC:
            try:
                os.unlink(seg_paths[-1])
            except OSError:
                pass
            seg_paths = seg_paths[:-1]
    if len(seg_paths) <= max_kept:
        return seg_paths
    scored: List[Tuple[float, str]] = []
    for p in seg_paths:
        lufs = _segment_lufs(p)
        # Negative distance from target → higher score is better. Missing LUFS
        # readings (very short / corrupt segments) sink to the bottom.
        score = -abs((lufs if lufs is not None else -50.0) - TARGET_LUFS)
        scored.append((score, p))
    top = {p for _, p in sorted(scored, reverse=True)[:max_kept]}
    for p in seg_paths:
        if p not in top:
            try:
                os.unlink(p)
            except OSError:
                pass
    return [p for p in seg_paths if p in top]


def _fixed_time_split(ref_path: str, out_dir: str) -> List[str]:
    """Fallback when silence detection found nothing (e.g. pure-singing reference)."""
    pattern = os.path.join(out_dir, "segment_%03d.wav")
    cmd = [
        "ffmpeg", "-y", "-i", ref_path, "-f", "segment",
        "-segment_time", str(SEGMENT_TARGET_SEC),
        "-reset_timestamps", "1", "-c", "copy", pattern,
    ]
    _run_ffmpeg(cmd)
    return sorted(glob.glob(os.path.join(out_dir, "segment_*.wav")))


def _split_reference_into_segments(ref_path: str, out_dir: str) -> List[str]:
    """
    Split the cleaned reference into per-sentence-ish segments aligned to silence
    boundaries (target ~SEGMENT_TARGET_SEC, hard min/max bounds), then keep only
    the top MAX_SEGMENTS_KEPT by loudness quality. XTTS averages the speaker
    embedding across this list.
    """
    duration = _probe_duration_seconds(ref_path)
    if duration < SEGMENT_MIN_SOURCE_SECONDS:
        return []

    silences = _silence_intervals(ref_path)
    speech: List[Tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor + 0.05:
            speech.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if cursor < duration:
        speech.append((cursor, duration))

    if speech:
        seg_bounds = _pack_speech_into_segments(speech)
    else:
        seg_bounds = []

    out_paths: List[str] = []
    if seg_bounds:
        for i, (s, e) in enumerate(seg_bounds):
            path = os.path.join(out_dir, f"segment_{i:03d}.wav")
            cmd = [
                "ffmpeg", "-y", "-i", ref_path,
                "-ss", f"{s:.3f}", "-to", f"{e:.3f}",
                "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
                path,
            ]
            try:
                _run_ffmpeg(cmd)
                out_paths.append(path)
            except RuntimeError:
                continue

    if not out_paths:
        out_paths = _fixed_time_split(ref_path, out_dir)

    return _rank_and_trim_segments(out_paths)


def create_voice(name: str, description: str, source_audio_path: str) -> dict:
    voice_id = uuid.uuid4().hex[:12]
    vdir = _voice_path(voice_id)
    os.makedirs(vdir, exist_ok=True)
    ref_path = os.path.join(vdir, "reference.wav")
    try:
        _prepare_reference(source_audio_path, ref_path)
        segment_paths = _split_reference_into_segments(ref_path, vdir)
    except Exception:
        shutil.rmtree(vdir, ignore_errors=True)
        raise

    meta = {
        "voice_id": voice_id,
        "name": name,
        "description": description,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "reference_seconds": round(_probe_duration_seconds(ref_path), 2),
        "segment_count": len(segment_paths),
    }
    with open(os.path.join(vdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def list_voices() -> list[dict]:
    out = []
    if not os.path.isdir(VOICES_DIR):
        return out
    for vid in sorted(os.listdir(VOICES_DIR)):
        meta_file = os.path.join(VOICES_DIR, vid, "meta.json")
        if os.path.isfile(meta_file):
            try:
                with open(meta_file) as f:
                    out.append(json.load(f))
            except Exception:
                continue
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def get_voice(voice_id: str) -> Optional[dict]:
    meta_file = os.path.join(_voice_path(voice_id), "meta.json")
    if not os.path.isfile(meta_file):
        return None
    with open(meta_file) as f:
        return json.load(f)


def get_reference_wav(voice_id: str) -> Optional[str]:
    """Legacy helper. Returns the single reference.wav path. Prefer get_reference_paths()."""
    p = os.path.join(_voice_path(voice_id), "reference.wav")
    return p if os.path.isfile(p) else None


def get_reference_paths(voice_id: str) -> Optional[List[str]]:
    """
    Return the list of conditioning paths for XTTS:
      - if segment_*.wav exist (voice cloned with the new pipeline), return them all
      - else fall back to [reference.wav] for backward compatibility with
        voices that were cloned before the multi-segment upgrade.
    """
    vdir = _voice_path(voice_id)
    segments = sorted(glob.glob(os.path.join(vdir, "segment_*.wav")))
    if segments:
        return segments
    ref = os.path.join(vdir, "reference.wav")
    return [ref] if os.path.isfile(ref) else None


def delete_voice(voice_id: str) -> bool:
    vdir = _voice_path(voice_id)
    if os.path.isdir(vdir):
        shutil.rmtree(vdir)
        return True
    return False
