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
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import List, Optional

VOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "voices")
os.makedirs(VOICES_DIR, exist_ok=True)

# How much of the user's upload we keep after silence-trim. XTTS only conditions on
# ~6s internally but averaging across multiple clean clips noticeably helps fidelity.
MAX_REF_SECONDS = 300

# Length of each conditioning segment. ~30s gives a good mix of prosody without
# any single segment dominating the average.
SEGMENT_SECONDS = 30

# Don't bother splitting if the cleaned reference is shorter than this.
SEGMENT_MIN_SOURCE_SECONDS = 45


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


def _split_reference_into_segments(ref_path: str, out_dir: str) -> List[str]:
    """
    Split the cleaned reference into ~SEGMENT_SECONDS slices. Returns the list
    of segment paths. If the reference is too short, returns []. XTTS picks
    speaker conditioning from this list and averages the embeddings.
    """
    duration = _probe_duration_seconds(ref_path)
    if duration < SEGMENT_MIN_SOURCE_SECONDS:
        return []

    pattern = os.path.join(out_dir, "segment_%03d.wav")
    cmd = [
        "ffmpeg", "-y",
        "-i", ref_path,
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-reset_timestamps", "1",
        "-c", "copy",
        pattern,
    ]
    _run_ffmpeg(cmd)
    segments = sorted(glob.glob(os.path.join(out_dir, "segment_*.wav")))

    # Drop the trailing segment if it's very short — a 2-second tail-end clip
    # contains too little prosody and skews the embedding average.
    if segments and len(segments) > 1:
        tail_dur = _probe_duration_seconds(segments[-1])
        if tail_dur < SEGMENT_SECONDS * 0.4:
            try:
                os.unlink(segments[-1])
            except OSError:
                pass
            segments = segments[:-1]

    return segments


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
