"""Final preview service: 15-30s trial render reusing the final pipeline.

The preview uses the SAME filter/render pipeline as the full final
(music mode + mix, logo filter, subtitle ASS, offset, split,
volumes, resolution). Only start/duration/output differ.

Layout:
    /var/lib/loop-video-audio/jobs/<job_id>/preview/
        state.json            progress + params + config hash
        final-preview.mp4     last good preview (atomic rename)
        final-preview.tmp.mp4 in-progress render (deleted on fail/cancel)
        preview.ass           window-shifted burn file (regenerated per run)
        pid                   running ffmpeg pid (for cancel/guard)
        ffmpeg-preview.log    ffmpeg stderr

The worker itself lives in app.py (same pattern as process_mix);
this module holds paths, validation, windowed ASS, hashing and
cancel/status helpers. No import from app (no cycles).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import tempfile
import time
from pathlib import Path

PREVIEW_ALLOWED_DURATIONS = (15, 30)
PREVIEW_MIN_DURATION = 5
PREVIEW_MAX_DURATION = 60
PREVIEW_THREADS = 2

JOBS_DIR = Path("/var/lib/loop-video-audio/jobs")


# ----------------------------------------------------------
# Paths
# ----------------------------------------------------------

def get_preview_dir(job_id: str) -> Path:
    from services.job_lifecycle_service import validate_job_id
    validate_job_id(job_id)
    return JOBS_DIR / job_id / "preview"


def preview_paths(job_id: str) -> dict:
    d = get_preview_dir(job_id)
    return {
        "dir": d,
        "state": d / "state.json",
        "video": d / "final-preview.mp4",
        "tmp": d / "final-preview.tmp.mp4",
        "ass": d / "preview.ass",
        "pid": d / "pid",
        "log": d / "ffmpeg-preview.log",
        "cancel_flag": d / "cancel.flag",
    }


# ----------------------------------------------------------
# Validation
# ----------------------------------------------------------

def validate_preview_params(
    video_duration: float,
    start_seconds: float,
    duration: float,
) -> tuple[float, float]:
    """Return (start, duration) clamped to the video.

    Raises ValueError on invalid input.
    """
    try:
        start = float(start_seconds)
    except (TypeError, ValueError):
        raise ValueError("preview_start_seconds phải là số")

    try:
        dur = float(duration)
    except (TypeError, ValueError):
        raise ValueError("preview_duration phải là số")

    if video_duration is None or video_duration <= 0:
        raise ValueError("Không đọc được video duration")

    if start < 0 or start >= video_duration:
        raise ValueError(
            "preview_start_seconds phải >= 0 và < video duration"
        )

    if dur < PREVIEW_MIN_DURATION or dur > PREVIEW_MAX_DURATION:
        raise ValueError(
            f"preview_duration phải từ {PREVIEW_MIN_DURATION} "
            f"đến {PREVIEW_MAX_DURATION} giây"
        )

    remaining = video_duration - start
    dur = min(dur, remaining)
    if dur <= 0:
        raise ValueError("Không còn đủ video cho preview")

    return start, dur


def music_loop_offset(start_seconds: float, music_duration: float) -> float:
    """Loop phase of the music at the preview start.

    Final at time T plays music (T % M); the preview must play
    the same phase, never restart music from zero.
    """
    if not music_duration or music_duration <= 0:
        return 0.0
    return float(start_seconds) % float(music_duration)


# ----------------------------------------------------------
# Windowed preview ASS (option B: shift, never touch source)
# ----------------------------------------------------------

def build_preview_ass(
    job_id: str,
    start_seconds: float,
    duration: float,
) -> Path | None:
    """Generate preview/preview.ass for the window, or None.

    Uses the same derived fragments (split + global offset) as
    final. Only intersecting cues are kept, timestamps shifted
    by -start. working.srt is never modified.
    """
    from services.subtitle_editor_service import (
        atomic_write,
        get_entries,
        get_job_dir,
        get_meta,
        get_offset_ms,
    )
    from services.subtitle_layout_service import (
        layout_entries,
        render_ass_string,
    )

    meta = get_meta(job_id)
    if not meta.get("subtitle_enabled"):
        return None

    entries = get_entries(job_id)
    if not entries:
        return None

    offset_ms = get_offset_ms(job_id)
    layout = layout_entries(entries, offset_ms=offset_ms)

    start_ms = int(round(start_seconds * 1000))
    end_ms = start_ms + int(round(duration * 1000))

    windowed: list[dict] = []
    for cue in layout["cues"]:
        for frag in cue["fragments"]:
            if frag["end_ms"] <= start_ms or frag["start_ms"] >= end_ms:
                continue
            s = max(frag["start_ms"], start_ms) - start_ms
            e = min(frag["end_ms"], end_ms) - start_ms
            if e <= s:
                continue
            windowed.append({
                "start_ms": s,
                "end_ms": e,
                "text": frag["text"],
            })

    if not windowed:
        return None

    paths = preview_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    atomic_write(paths["ass"], render_ass_string(windowed))
    return paths["ass"]


# ----------------------------------------------------------
# Config hash (stale detection)
# ----------------------------------------------------------

def _file_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        return f"{path.name}:missing"


def compute_config_hash(
    *,
    job_id: str,
    music_mode: str,
    music_path: Path | None,
    base_volume: float,
    music_volume: float,
    start_seconds: float,
    duration: float,
) -> str:
    """Hash of everything the preview depends on."""
    from services.subtitle_editor_service import get_job_dir

    job_dir = JOBS_DIR / job_id
    sub_dir = get_job_dir(job_id)

    parts: list[str] = []

    logo_cfg = sub_dir.parent / "logo" / "config.json"
    try:
        parts.append(
            "logo:" + logo_cfg.read_bytes().decode("utf-8", "replace")
        )
    except OSError:
        parts.append("logo:missing")

    template_cfg = sub_dir.parent / "template" / "config.json"
    try:
        parts.append(
            "template:" + template_cfg.read_bytes().decode("utf-8", "replace")
        )
    except OSError:
        parts.append("template:missing")

    for name in ("source.svg", "source.png", "source.webp", "render.png"):
        parts.append(
            "template_file:"
            + _file_fingerprint(sub_dir.parent / "template" / name)
        )

    working = sub_dir / "working.srt"
    try:
        parts.append(
            "srt:" + working.read_bytes().decode("utf-8", "replace")
        )
    except OSError:
        parts.append("srt:missing")

    from services.subtitle_editor_service import get_offset_ms
    parts.append(f"offset:{get_offset_ms(job_id)}")

    parts.append(f"music_mode:{music_mode}")
    if music_path is not None:
        parts.append("music:" + _file_fingerprint(Path(music_path)))
    else:
        parts.append("music:none")

    parts.append(f"volumes:{base_volume}:{music_volume}")
    parts.append(f"window:{start_seconds}:{duration}")

    return hashlib.md5(
        "\n".join(parts).encode("utf-8")
    ).hexdigest()


# ----------------------------------------------------------
# State / status
# ----------------------------------------------------------

def read_preview_state(job_id: str) -> dict | None:
    path = preview_paths(job_id)["state"]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pid(job_id: str) -> int | None:
    try:
        return int(
            preview_paths(job_id)["pid"].read_text(
                encoding="utf-8"
            ).strip()
        )
    except (OSError, ValueError):
        return None


def is_preview_running(job_id: str) -> bool:
    """Single-flight guard: at most one preview ffmpeg per job.

    "processing" requires a live pid (crash recovery otherwise).
    "queued" counts as running while fresh (worker committed but
    not yet spawned ffmpeg); stale queued entries expire so a
    dead worker can never block the slot forever.
    """
    state = read_preview_state(job_id)
    if not state:
        return False

    status = state.get("status")

    if status == "processing":
        return pid_alive(read_pid(job_id))

    if status == "queued":
        try:
            age = time.time() - float(state.get("started_at", 0))
        except (TypeError, ValueError):
            return True
        return age < 120

    return False


def get_preview_status(job_id: str) -> dict:
    """Status response incl. stale flag (404-style when never run)."""
    from services.subtitle_editor_service import get_offset_ms  # noqa

    state = read_preview_state(job_id)
    if state is None:
        return {"status": "none", "stale": False}

    stale = False
    if state.get("status") == "done" and state.get("config_hash"):
        try:
            current = compute_config_hash(
                job_id=job_id,
                music_mode=state.get("music_mode", "none"),
                music_path=state.get("music_path"),
                base_volume=state.get("base_volume", 100),
                music_volume=state.get("music_volume", 15),
                start_seconds=state.get("start_seconds", 0),
                duration=state.get("duration", 0),
            )
            stale = current != state.get("config_hash")
        except Exception:
            stale = False

    import time as _time
    elapsed = state.get("elapsed_seconds", 0)
    started = state.get("started_at")
    if state.get("status") == "processing" and started:
        try:
            elapsed = max(0.0, _time.time() - float(started))
        except (TypeError, ValueError):
            pass

    return {
        "status": state.get("status"),
        "progress": state.get("progress", 0),
        "stale": stale,
        "start_seconds": state.get("start_seconds"),
        "duration": state.get("duration"),
        "rendered_time": state.get("rendered_seconds"),
        "runtime_seconds": round(elapsed, 1),
        "eta_seconds": state.get("eta_seconds"),
        "message": state.get("message"),
        "error": state.get("error"),
    }


# ----------------------------------------------------------
# Cancel
# ----------------------------------------------------------

def cancel_preview(job_id: str) -> dict:
    """SIGTERM the preview ffmpeg; mark cancelled; drop tmp file."""
    paths = preview_paths(job_id)
    state = read_preview_state(job_id)

    if state is None or state.get("status") != "processing":
        return {"ok": True, "status": state.get("status") if state else "none"}

    pid = read_pid(job_id)
    if pid_alive(pid):
        # Flag FIRST so the worker (which may already be in its
        # except block) keeps "cancelled" instead of "failed".
        try:
            paths["cancel_flag"].write_text("1", encoding="utf-8")
        except OSError:
            pass

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    try:
        paths["tmp"].unlink(missing_ok=True)
    except OSError:
        pass

    try:
        state = read_preview_state(job_id) or {}
        state.update({
            "status": "cancelled",
            "progress": 0,
            "message": "Đã hủy preview.",
        })
        tmp = paths["state"].with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(paths["state"])
    except OSError:
        pass

    return {"ok": True, "status": "cancelled"}


# ----------------------------------------------------------
# Atomic state write (local helper, no app import)
# ----------------------------------------------------------

def write_preview_state(job_id: str, data: dict) -> None:
    paths = preview_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(paths["dir"]), prefix="pvstate_"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, paths["state"])
