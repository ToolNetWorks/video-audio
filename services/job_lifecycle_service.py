"""Job lifecycle + production cleanup.

Rules:
- Everything under /var/lib/loop-video-audio/jobs/<job_id>/ is TEMPORARY
  (uploads, SRT, preview, intermediate, final) and dies with the job.
- NEVER touched: assets/, Drive models, source code, /tmp cache that
  belongs to system assets.
- Jobs are swept JOB_TTL_HOURS after they reach a terminal state
  (done/failed/cancelled) or after creation when they never finish.
- Hung workers (processing older than PROCESSING_TIMEOUT_HOURS) are
  marked failed so they become sweepable.

All knobs are env-overridable. No import from app (no cycles).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

JOBS_DIR = Path(
    os.getenv(
        "LOOP_JOBS_DIR",
        "/var/lib/loop-video-audio/jobs",
    )
)

JOB_TTL_HOURS = float(os.getenv("JOB_TTL_HOURS", "72"))
SWEEP_INTERVAL_MIN = float(os.getenv("SWEEP_INTERVAL_MIN", "30"))
PROCESSING_TIMEOUT_HOURS = float(
    os.getenv("PROCESSING_TIMEOUT_HOURS", "12")
)
MAX_CONCURRENT_FFMPEG = int(os.getenv("MAX_CONCURRENT_FFMPEG", "2"))
MIN_FREE_BYTES = int(os.getenv("MIN_FREE_BYTES", str(2 * 1024**3)))
TMP_TTL_HOURS = float(os.getenv("TMP_TTL_HOURS", "24"))

# Upload caps (bytes).
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(4 * 1024**3)))
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(2 * 1024**3)))
MAX_MUSIC_BYTES = int(os.getenv("MAX_MUSIC_BYTES", str(512 * 1024**2)))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(20 * 1024**2)))
MAX_SRT_BYTES = int(os.getenv("MAX_SRT_BYTES", str(10 * 1024**2)))

# job_id may only be a single safe path segment (uuid hex in
# practice, plus legacy test ids). Blocks "..", "/", quotes, etc.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

# Leftover chunk prefixes from atomic writes + template thumbs.
TMP_PREFIXES = ("srt_", "logo_", "tpl_", "pvstate_", "tmp")
TEMPLATE_THUMB_DIR = Path("/tmp/loop-video-audio-template-thumb")


# ----------------------------------------------------------
# Identity / paths
# ----------------------------------------------------------

def validate_job_id(job_id: str) -> str:
    """Return job_id or raise ValueError (path traversal guard)."""
    if not job_id or not JOB_ID_RE.match(job_id):
        raise ValueError("Job không tồn tại")
    return job_id


def job_dir(job_id: str) -> Path:
    """Validated job directory (never escapes JOBS_DIR)."""
    validate_job_id(job_id)
    path = JOBS_DIR / job_id
    # Belt and suspenders: resolve must stay inside JOBS_DIR.
    try:
        path.resolve().relative_to(JOBS_DIR.resolve())
    except (ValueError, OSError):
        raise ValueError("Job không tồn tại")
    return path


# ----------------------------------------------------------
# Timestamps
# ----------------------------------------------------------

def _now() -> float:
    return time.time()


def _read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix="tmp"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, path)


def stamp_created(state_path: Path) -> None:
    state = _read_state(state_path) or {}
    state.setdefault("created_at", _now())
    _atomic_write_json(state_path, state)


def stamp_terminal(state_path: Path, status: str) -> None:
    state = _read_state(state_path) or {}
    state["status"] = status
    state.setdefault("created_at", _now())
    state["completed_at"] = _now()
    _atomic_write_json(state_path, state)


def job_terminal_time(job_path: Path) -> float | None:
    """Epoch when the job finished, or None when still active."""
    state = _read_state(job_path / "state.json")
    if not state:
        return None
    if state.get("status") in TERMINAL_STATES:
        completed = state.get("completed_at")
        try:
            return float(completed) if completed else None
        except (TypeError, ValueError):
            return None
    return None


def job_age_basis(job_path: Path) -> float:
    """completed_at, else created_at, else dir mtime (fallback)."""
    state = _read_state(job_path / "state.json") or {}
    for key in ("completed_at", "created_at"):
        try:
            value = state.get(key)
            if value:
                return float(value)
        except (TypeError, ValueError):
            continue
    try:
        return job_path.stat().st_mtime
    except OSError:
        return _now()


# ----------------------------------------------------------
# Disk guard
# ----------------------------------------------------------

def free_bytes(path: Path = JOBS_DIR) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def require_free_space(min_bytes: int = MIN_FREE_BYTES) -> None:
    """Raise RuntimeError (→ 507) when disk is too full."""
    if free_bytes() < min_bytes:
        raise RuntimeError(
            "Ổ đĩa sắp đầy, thử lại sau khi dọn dẹp"
        )


# ----------------------------------------------------------
# FFmpeg processes
# ----------------------------------------------------------

def _iter_ffmpeg_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "ffmpeg"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    me = os.getpid()
    for line in out.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != me:
            pids.append(pid)
    return pids


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
    except OSError:
        return ""


def _job_pids(job_id: str) -> list[int]:
    """FFmpeg pids whose cmdline references this job."""
    needle = f"/{job_id}/"
    found = []
    for pid in _iter_ffmpeg_pids():
        if needle in _cmdline(pid):
            found.append(pid)
    return found


def _all_job_ffmpeg_pids() -> list[int]:
    """FFmpeg pids referencing ANY job dir (post-restart orphans)."""
    needle = str(JOBS_DIR) + "/"
    return [
        pid for pid in _iter_ffmpeg_pids()
        if needle in _cmdline(pid)
    ]


def _terminate(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def kill_job_processes(job_id: str, wait_s: float = 5.0) -> int:
    """SIGTERM then SIGKILL ffmpeg belonging to a job. Returns kills."""
    pids = _job_pids(job_id)
    for pid in pids:
        _terminate(pid)
    deadline = _now() + wait_s
    pending = list(pids)
    while pending and _now() < deadline:
        pending = [p for p in pending if _pid_alive(p)]
        if pending:
            time.sleep(0.2)
    for pid in pending:
        _terminate(pid, signal.SIGKILL)
    return len(pids)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_startup_orphans() -> int:
    """Kill ffmpeg leftovers from before a restart (all are orphans).

    Must run at startup, before this process spawns any worker.
    """
    pids = _all_job_ffmpeg_pids()
    for pid in pids:
        _terminate(pid)
    deadline = _now() + 5.0
    pending = list(pids)
    while pending and _now() < deadline:
        pending = [p for p in pending if _pid_alive(p)]
        if pending:
            time.sleep(0.2)
    for pid in pending:
        _terminate(pid, signal.SIGKILL)
    return len(pids)


# ----------------------------------------------------------
# Startup reconciliation + sweeps
# ----------------------------------------------------------

def reconcile_startup() -> dict:
    """Fix states left hanging by a restart/crash.

    Returns {"reaped": n, "marked": n}.
    """
    reaped = reap_startup_orphans()
    marked = 0
    if not JOBS_DIR.exists():
        return {"reaped": reaped, "marked": marked}
    for child in JOBS_DIR.iterdir():
        if not child.is_dir():
            continue
        for name in ("state.json", "mix.json"):
            path = child / name
            state = _read_state(path)
            if not state:
                continue
            if state.get("status") in ("processing", "queued"):
                state["status"] = "failed"
                state["error"] = (
                    "Service restarted giữa chừng, "
                    "vui lòng chạy lại."
                )
                state.setdefault("created_at", _now())
                state["completed_at"] = _now()
                try:
                    _atomic_write_json(path, state)
                    marked += 1
                except OSError:
                    pass
        preview_state = child / "preview" / "state.json"
        state = _read_state(preview_state)
        if state and state.get("status") in ("processing", "queued"):
            state["status"] = "failed"
            state["error"] = (
                "Service restarted giữa chừng, "
                "vui lòng chạy lại."
            )
            try:
                _atomic_write_json(preview_state, state)
                marked += 1
            except OSError:
                pass
    return {"reaped": reaped, "marked": marked}


def sweep_processing_timeouts() -> int:
    """Mark hung workers (processing too long) as failed."""
    if PROCESSING_TIMEOUT_HOURS <= 0:
        return 0
    limit = PROCESSING_TIMEOUT_HOURS * 3600
    now = _now()
    marked = 0
    if not JOBS_DIR.exists():
        return 0
    for child in JOBS_DIR.iterdir():
        if not child.is_dir():
            continue
        targets = [
            child / "state.json",
            child / "mix.json",
            child / "preview" / "state.json",
        ]
        for path in targets:
            state = _read_state(path)
            if not state or state.get("status") != "processing":
                continue
            try:
                started = float(state.get("started_at", 0) or 0)
            except (TypeError, ValueError):
                started = 0
            if started and now - started < limit:
                continue
            state["status"] = "failed"
            state["error"] = (
                "Worker treo quá lâu, đã dừng để dọn dẹp."
            )
            state.setdefault("created_at", now)
            state["completed_at"] = now
            try:
                _atomic_write_json(path, state)
                marked += 1
            except OSError:
                pass
    return marked


def sweep_expired_jobs(now: float | None = None) -> list[str]:
    """Delete job dirs past TTL. Returns removed job ids."""
    removed: list[str] = []
    if JOB_TTL_HOURS <= 0 or not JOBS_DIR.exists():
        return removed
    now = now if now is not None else _now()
    ttl = JOB_TTL_HOURS * 3600
    for child in JOBS_DIR.iterdir():
        if not child.is_dir():
            continue
        terminal = job_terminal_time(child)
        if terminal is None:
            # Active job: only swept by age when ancient (safety).
            basis = job_age_basis(child)
            if now - basis < ttl * 2:
                continue
        elif now - terminal < ttl:
            continue
        kill_job_processes(child.name, wait_s=2.0)
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed


def sweep_tmp_files(now: float | None = None) -> int:
    """Delete stale temp leftovers (crash orphans)."""
    if TMP_TTL_HOURS <= 0:
        return 0
    now = now if now is not None else _now()
    limit = TMP_TTL_HOURS * 3600
    removed = 0

    def _old(path: Path) -> bool:
        try:
            return now - path.stat().st_mtime > limit
        except OSError:
            return False

    for base in (Path("/tmp"), TEMPLATE_THUMB_DIR):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for child in entries:
            try:
                if not child.is_file():
                    continue
                name = child.name
                if base == TEMPLATE_THUMB_DIR or name.startswith(
                    TMP_PREFIXES
                ) or name.endswith((".tmp",)):
                    if _old(child):
                        child.unlink(missing_ok=True)
                        removed += 1
            except OSError:
                continue

    # Stale preview tmp renders without a live worker.
    if JOBS_DIR.exists():
        for job in JOBS_DIR.iterdir():
            tmp = job / "preview" / "final-preview.tmp.mp4"
            try:
                if tmp.is_file() and _old(tmp):
                    tmp.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed


def sweep_once() -> dict:
    """One full cleanup cycle. Never raises."""
    result: dict = {
        "timeouts": 0,
        "expired_jobs": [],
        "tmp_files": 0,
        "free_bytes": free_bytes(),
    }
    try:
        result["timeouts"] = sweep_processing_timeouts()
    except Exception:
        pass
    try:
        result["expired_jobs"] = sweep_expired_jobs()
    except Exception:
        pass
    try:
        result["tmp_files"] = sweep_tmp_files()
    except Exception:
        pass
    try:
        result["free_bytes"] = free_bytes()
    except Exception:
        pass
    return result


def delete_job(job_id: str) -> bool:
    """Kill workers and remove the whole job dir. Returns existed."""
    path = job_dir(job_id)
    existed = path.exists()
    kill_job_processes(job_id)
    shutil.rmtree(path, ignore_errors=True)
    return existed
