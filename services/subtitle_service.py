from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

logger = logging.getLogger("subtitle-service")

LOG_DIR = Path("/var/lib/loop-video-audio/logs")
COLAB_SESSION = os.getenv("COLAB_SESSION", "subtitle")
COLAB_TIMEOUT = int(os.getenv("COLAB_TIMEOUT", "14400")) # 4 hours for full job
SUBTITLE_TOKEN_TTL = int(os.getenv("SUBTITLE_TOKEN_TTL", "14400"))
DATA_DIR = Path("/var/lib/loop-video-audio/jobs")

SUBTITLE_AUDIO_TOKENS: dict[str, dict] = {}

def _write_log(job_dir: Path, msg: str) -> None:
    subtitle_dir = _get_subtitle_dir(job_dir)
    log_file = subtitle_dir / "subtitle.log"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {msg}\n")
    logger.info(f"[{job_dir.name}] {msg}")

def _validate_job_id(job_id: str) -> Path:
    if not job_id or not job_id.isalnum():
        raise HTTPException(400, "Job ID không hợp lệ")
    job_dir = DATA_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(404, "Job không tồn tại")
    return job_dir

def _find_audio(job_dir: Path) -> Path:
    for ext in (".mp3", ".wav", ".m4a", ".mp4"):
        audio_path = job_dir / f"input_audio{ext}"
        if audio_path.exists():
            return audio_path
    raise HTTPException(404, "Không tìm thấy audio gốc")

def _get_subtitle_dir(job_dir: Path) -> Path:
    d = job_dir / "subtitle"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _write_state(state_path: Path, state: dict) -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)

def _create_audio_token(job_id: str, audio_path: Path) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    SUBTITLE_AUDIO_TOKENS[token_hash] = {
        "job_id": job_id,
        "expires": datetime.now(timezone.utc) + timedelta(seconds=SUBTITLE_TOKEN_TTL),
        "audio_path": str(audio_path),
    }
    return token, token_hash

def _validate_token(token: str, job_id: str) -> Path:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    entry = SUBTITLE_AUDIO_TOKENS.get(token_hash)
    if not entry:
        raise HTTPException(403, "Token không hợp lệ")
    if entry["job_id"] != job_id:
        raise HTTPException(403, "Token không khớp job")
    if datetime.now(timezone.utc) > entry["expires"]:
        SUBTITLE_AUDIO_TOKENS.pop(token_hash, None)
        raise HTTPException(403, "Token đã hết hạn")
    return Path(entry["audio_path"])

def _revoke_token(token: str) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    SUBTITLE_AUDIO_TOKENS.pop(token_hash, None)

def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _run_colab(args: list[str], timeout: int, job_dir: Path = None, state_updater=None) -> None:
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            if job_dir:
                _write_log(job_dir, f"[COLAB] {line}")
            if state_updater and line.startswith("PROGRESS_JSON:"):
                try:
                    data = json.loads(line.replace("PROGRESS_JSON:", "", 1))
                    state_updater(data)
                except Exception:
                    pass
        proc.stdout.close()
        ret = proc.wait(timeout=timeout)
        if ret != 0:
            raise RuntimeError(f"Lệnh Colab thất bại (exit code {ret})")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Quá thời gian thực thi lệnh Colab") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("Không tìm thấy lệnh `colab` trên VPS.") from exc

def _colab_download(remote: str, local: Path) -> None:
    logger.info("colab download: %s -> %s", remote, local.name)
    local.parent.mkdir(parents=True, exist_ok=True)
    _run_colab(["colab", "download", "-s", COLAB_SESSION, remote, str(local)], timeout=180)

def start_subtitle_job(job_id: str, model: str) -> dict:
    job_dir = _validate_job_id(job_id)
    source_audio = _find_audio(job_dir)
    subtitle_dir = _get_subtitle_dir(job_dir)
    state_path = subtitle_dir / "state.json"
    
    public_url = os.getenv("PUBLIC_BASE_URL")
    if not public_url:
        raise HTTPException(500, "PUBLIC_BASE_URL không được cấu hình. Cần PUBLIC_BASE_URL để Colab có thể tải audio.")
        
    from services.drive_auth_manager import auth_manager
    auth_manager.check_status()
    if not auth_manager.state["drive_mounted"]:
        raise HTTPException(status_code=400, detail={"code": "DRIVE_AUTH_REQUIRED"})

    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        status = state.get("status", "")
        if status in ("queued", "preparing", "downloading_audio", "probing_audio", "loading_model", "transcribing", "merging"):
            raise HTTPException(409, "Subtitle job đang chạy.")
        if status == "failed":
            state_path.unlink(missing_ok=True)

    token, token_hash = _create_audio_token(job_id, source_audio)
    audio_url = f"{public_url}/api/internal/subtitle-audio/{job_id}?token={token}"

    state = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0.0,
        "model": model,
        "language": "vi",
        "audio_duration": 0.0,
        "processed_seconds": 0.0,
        "runtime_seconds": 0.0,
        "eta_seconds": None,
        "segments": 0,
        "message": "Đang chuẩn bị...",
        "audio_token_hash": token_hash,
        "chunk_duration": 600,
        "overlap": 2,
        "started_at": time.time()
    }
    _write_state(state_path, state)
    _write_log(job_dir, f"START model={model} audio={source_audio.name}")

    threading.Thread(
        target=_run_subtitle_job,
        args=(job_id, model, source_audio, audio_url, token, state_path),
        daemon=True,
    ).start()

    return {"subtitle_job_id": job_id, "status": "queued"}

def _run_subtitle_job(job_id: str, model: str, source_audio: Path, audio_url: str, token: str, state_path: Path) -> None:
    job_dir = DATA_DIR / job_id
    subtitle_dir = _get_subtitle_dir(job_dir)

    def set_state(update: dict) -> None:
        state = {"job_id": job_id}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        state.update(update)
        _write_state(state_path, state)
        
    def handle_progress(data: dict):
        stage = data.get("stage", "")
        update = {"status": stage}
        if "progress" in data:
            update["progress"] = data["progress"]
        if stage == "transcribing":
            processed = data.get("processed_seconds", 0)
            update["processed_seconds"] = processed
            update["current_chunk"] = data.get("chunk", 0)
            update["total_chunks"] = data.get("total_chunks", 0)
            update["audio_duration"] = data.get("total_duration", 0)
            update["message"] = f"Đang nhận dạng chunk {update['current_chunk']}/{update['total_chunks']}..."
            
            # calculate ETA
            if update["audio_duration"] > 0 and processed > 0:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                started = state_data.get("started_at", time.time())
                elapsed = time.time() - started
                speed = processed / elapsed
                if speed > 0:
                    update["eta_seconds"] = (update["audio_duration"] - processed) / speed
        elif stage == "merging":
            update["message"] = "Đang merge kết quả..."
        elif stage == "uploading_result":
            update["message"] = "Đang tải kết quả về VPS..."
            
        set_state(update)

    started = time.time()
    try:
        set_state({"status": "preparing", "progress": 0.0, "message": "Đang chuẩn bị gửi lệnh sang Colab..."})
        
        config = {
            "job_id": job_id,
            "audio_url": audio_url,
            "model": model,
            "language": "vi",
            "chunk_duration": 600,
            "overlap": 2
        }
        
        runner_code = (Path(__file__).parent.parent / "colab" / "runner.py").read_text(encoding="utf-8")
        runner_path = subtitle_dir / "runner.py"
        runner_code = f"CONFIG_JSON = '''{json.dumps(config)}'''\n" + runner_code
        runner_path.write_text(runner_code, encoding="utf-8")
        
        _write_log(job_dir, "Executing colab runner for full ASR process...")
        _run_colab(["colab", "exec", "-s", COLAB_SESSION, "-f", str(runner_path)], timeout=COLAB_TIMEOUT, job_dir=job_dir, state_updater=handle_progress)
        
        # Download result
        remote_srt = f"/content/loop-video-audio/{job_id}/subtitle.srt"
        local_srt = subtitle_dir / "subtitle.srt"
        _colab_download(remote_srt, local_srt)
        
        if not local_srt.exists():
            raise RuntimeError("Không tìm thấy subtitle.srt sau khi Colab chạy xong.")
            
        from services.subtitle_editor_service import setup_subtitle_files
        setup_subtitle_files(job_id, "generated", local_srt.read_text(encoding="utf-8"))
            
        elapsed = time.time() - started
        set_state({
            "status": "done",
            "progress": 100.0,
            "message": "Subtitle đã tạo",
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_text": format_duration(elapsed),
            "eta_seconds": 0,
            "eta_text": "00:00:00",
        })
        _write_log(job_dir, "DONE")
        
    except Exception as exc:
        elapsed = time.time() - started
        _write_log(job_dir, f"FAILED: {exc}")
        set_state({
            "status": "failed",
            "progress": 0.0,
            "message": "Tạo SRT thất bại.",
            "error": str(exc),
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_text": format_duration(elapsed),
        })
    finally:
        _revoke_token(token)


def get_model_status() -> dict:
    runner_path = Path(__file__).parent.parent / "colab" / "model_manager.py"
    if not runner_path.exists():
        return {}
    
    config = json.dumps({"action": "status", "model_name": "gipformer1.5-68M-rnnt"})
    # Run the colab command
    try:
        proc = subprocess.run(
            ["colab", "exec", "-s", COLAB_SESSION, "-f", str(runner_path)],
            input=config,
            text=True,
            capture_output=True,
            timeout=15
        )
        # Parse the last json line
        lines = proc.stdout.strip().split('\n')
        for line in reversed(lines):
            try:
                data = json.loads(line.strip())
                if "engine" in data:
                    return {"gipformer": data}
            except:
                pass
    except Exception:
        pass
    
    return {"gipformer": {"install_status": "Checking..."}}

def install_model_drive(model_name: str) -> dict:
    runner_path = Path(__file__).parent.parent / "colab" / "model_manager.py"
    config = json.dumps({"action": "install", "model_name": model_name})
    
    # We can run it in a background thread or let colab exec handle it blocking
    # The UI will poll status. So let's run it non-blocking using threading
    def run_install():
        try:
            subprocess.run(
                ["colab", "exec", "-s", COLAB_SESSION, "-f", str(runner_path)],
                input=config,
                text=True,
                timeout=600
            )
        except Exception:
            pass
            
    threading.Thread(target=run_install, daemon=True).start()
    return {"status": "started"}

def get_subtitle_status(job_id: str) -> dict:
    job_dir = _validate_job_id(job_id)
    subtitle_dir = _get_subtitle_dir(job_dir)
    state_path = subtitle_dir / "state.json"

    if not state_path.exists():
        raise HTTPException(404, "Chưa có subtitle job")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    status = state.get("status", "queued")
    elapsed = state.get("elapsed_seconds", 0.0)
    started = state.get("started_at")

    if status not in ("done", "failed") and started:
        elapsed = max(0.0, time.time() - float(started))

    return {
        "status": status,
        "progress": round(state.get("progress", 0.0), 1),
        "model": state.get("model"),
        "language": state.get("language", "vi"),
        "audio_duration_seconds": round(state.get("audio_duration", 0.0), 1),
        "audio_duration_text": format_duration(state.get("audio_duration", 0.0)),
        "processed_seconds": round(state.get("processed_seconds", 0.0), 1),
        "processed_time_text": format_duration(state.get("processed_seconds", 0.0)),
        "runtime_seconds": round(elapsed, 1),
        "runtime_text": format_duration(elapsed),
        "eta_seconds": round(state["eta_seconds"], 1) if state.get("eta_seconds") is not None else None,
        "eta_text": format_duration(state["eta_seconds"]) if state.get("eta_seconds") is not None else None,
        "segments": state.get("segments", 0),
        "message": state.get("message", ""),
        "error": state.get("error"),
        "total_chunks": state.get("total_chunks", 0),
        "current_chunk": state.get("current_chunk"),
    }

def get_subtitle_content(job_id: str) -> str:
    job_dir = _validate_job_id(job_id)
    subtitle_dir = _get_subtitle_dir(job_dir)
    srt_path = subtitle_dir / "subtitle.srt"
    if not srt_path.exists():
        raise HTTPException(404, "Chưa có SRT")
    return srt_path.read_text(encoding="utf-8")

def reset_subtitle_state(job_id: str) -> None:
    job_dir = _validate_job_id(job_id)
    subtitle_dir = _get_subtitle_dir(job_dir)
    state_path = subtitle_dir / "state.json"
    if state_path.exists():
        state_path.unlink(missing_ok=True)

def retry_chunk(job_id: str, chunk_index: int) -> dict:
    raise HTTPException(400, "Chức năng retry từng chunk đã bị loại bỏ vì toàn bộ quá trình giờ chạy một mạch trên Colab.")

def cancel_subtitle_job(job_id: str) -> None:
    job_dir = _validate_job_id(job_id)
    subtitle_dir = _get_subtitle_dir(job_dir)
    state_path = subtitle_dir / "state.json"
    
    if not state_path.exists():
        raise HTTPException(404, "Chưa có subtitle job")
        
    state = json.loads(state_path.read_text(encoding="utf-8"))
    status = state.get("status", "")
    
    if status in ("done", "failed"):
        raise HTTPException(400, f"Job đã hoàn tất (trạng thái: {status}), không thể hủy.")
        
    # revoke token
    token_hash = state.get("audio_token_hash")
    if token_hash:
        SUBTITLE_AUDIO_TOKENS.pop(token_hash, None)
        
    state["status"] = "failed"
    state["error"] = "Bị hủy bởi người dùng"
    state["message"] = "Bị hủy"
    _write_state(state_path, state)
    
    _write_log(job_dir, "JOB CANCELLED BY USER")
    
    # Try to stop colab session if needed, but since it's a runner process, 
    # we can't easily kill the specific process without process IDs.
    # We can restart the kernel via colab CLI as a way to abort execution.
    try:
        subprocess.run(["colab", "restart-kernel", "-s", COLAB_SESSION], timeout=30)
    except Exception:
        pass
