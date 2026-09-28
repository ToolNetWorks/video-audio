from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

COLAB_SESSION = os.getenv("COLAB_SESSION", "subtitle")
MODEL_DRIVE_DIR = "/content/gdrive/MyDrive/loop-video-audio/models"
MODEL_LOCAL_DIR = "/content/models"
COLAB_CHECK_TIMEOUT = 30


def _run_colab(
    args: list[str],
    timeout: int = COLAB_CHECK_TIMEOUT,
    input: str | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -2, "", "colab_not_found"
    except Exception as exc:
        return -3, "", str(exc)


def check_colab_session() -> dict[str, Any]:
    status = {
        "colab_connected": False,
        "colab_session": COLAB_SESSION,
        "colab_error": None,
    }

    ret, stdout, stderr = _run_colab(["colab", "sessions"], timeout=30)
    if ret != 0:
        status["colab_error"] = stderr or stdout or "colab sessions failed"
        return status

    session_marker = f"[{COLAB_SESSION}]"
    if session_marker not in stdout:
        status["colab_error"] = "Session not found"
        return status

    status["colab_connected"] = True
    status["colab_error"] = None
    return status


def check_drive_mounted() -> dict[str, Any]:
    result = {
        "drive_mounted": False,
        "drive_path": "/content/gdrive/MyDrive",
        "drive_error": None,
    }

    colab = check_colab_session()
    if not colab["colab_connected"]:
        result["drive_error"] = "Colab session not connected"
        return result

    script_path = "/tmp/check_drive.py"
    try:
        Path(script_path).write_text("import os; print('OK' if os.path.exists('/content/gdrive/MyDrive') else 'NO')", encoding="utf-8")
        ret, stdout, stderr = _run_colab(
            ["colab", "exec", "-s", COLAB_SESSION, "-f", script_path],
            timeout=COLAB_CHECK_TIMEOUT,
        )
        if ret == -1:
            result["drive_error"] = "timeout"
            return result
        if ret == -2:
            result["drive_error"] = "colab_not_found"
            return result
        if ret != 0:
            result["drive_error"] = stderr or stdout or "exec failed"
            return result

        result["drive_mounted"] = "OK" in stdout
        if not result["drive_mounted"]:
            result["drive_error"] = "Drive not mounted"
    except Exception as exc:
        result["drive_error"] = str(exc)
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)
        except Exception:
            pass

    return result


def check_model_on_drive(model_name: str) -> dict[str, Any]:
    result = {
        "model_name": model_name,
        "model_on_drive": False,
        "model_local": False,
        "model_error": None,
    }

    colab = check_colab_session()
    if not colab["colab_connected"]:
        result["model_error"] = "Colab session not connected"
        return result

    drive_path = f"{MODEL_DRIVE_DIR}/{model_name}"
    local_path = f"{MODEL_LOCAL_DIR}/{model_name}"

    script = (
        "import os, json\n"
        f"drive='{drive_path}'\n"
        f"local='{local_path}'\n"
        "print(json.dumps({\n"
        "  'drive': os.path.isdir(drive),\n"
        "  'local': os.path.isdir(local)\n"
        "}))\n"
    )

    script_path = "/tmp/check_model.py"
    try:
        Path(script_path).write_text(script, encoding="utf-8")
        ret, stdout, stderr = _run_colab(
            ["colab", "exec", "-s", COLAB_SESSION, "-f", script_path],
            timeout=COLAB_CHECK_TIMEOUT,
        )
        if ret in (-1, -2):
            result["model_error"] = "colab_timeout_or_missing"
            return result
        if ret != 0:
            result["model_error"] = stderr or stdout or "model_check_failed"
            return result

        try:
            data = json.loads(stdout.strip().splitlines()[-1])
            result["model_on_drive"] = bool(data.get("drive"))
            result["model_local"] = bool(data.get("local"))
        except Exception as exc:
            result["model_error"] = str(exc)
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)
        except Exception:
            pass

    return result


def get_subtitle_runtime_status(selected_model: str = "auto") -> dict[str, Any]:
    colab = check_colab_session()
    drive = check_drive_mounted()
    model_status = None

    resolved_model = selected_model
    if selected_model == "auto":
        resolved_model = "auto"

    if resolved_model != "auto" and colab["colab_connected"]:
        model_status = check_model_on_drive(resolved_model)

    ready_for_asr = bool(
        colab["colab_connected"]
        and drive["drive_mounted"]
        and (
            resolved_model == "auto"
            or (model_status and model_status.get("model_on_drive"))
        )
    )

    message = "Sẵn sàng tạo phụ đề"
    if not colab["colab_connected"]:
        message = "Phiên Colab chưa kết nối"
    elif not drive["drive_mounted"]:
        message = "Cần kết nối Google Drive"
    elif resolved_model != "auto" and model_status and not model_status.get("model_on_drive"):
        message = f"Model {resolved_model} chưa có trên Drive"

    return {
        "colab_connected": colab["colab_connected"],
        "colab_session": COLAB_SESSION,
        "colab_error": colab.get("colab_error"),
        "drive_mounted": drive["drive_mounted"],
        "drive_path": drive.get("drive_path"),
        "drive_error": drive.get("drive_error"),
        "selected_model": resolved_model,
        "model_on_drive": model_status.get("model_on_drive") if model_status else None,
        "model_local": model_status.get("model_local") if model_status else None,
        "model_error": model_status.get("model_error") if model_status else None,
        "auth_required": not drive["drive_mounted"],
        "oauth_url": None,
        "ready_for_asr": ready_for_asr,
        "message": message,
    }
