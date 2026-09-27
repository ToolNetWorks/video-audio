import sys
import os
import json
import shutil
from pathlib import Path

# Config passed via stdin
config = {}
try:
    config_text = sys.stdin.read().strip()
    if config_text:
        config = json.loads(config_text)
except:
    pass

action = config.get("action", "status")
model_name = config.get("model_name", "gipformer1.5-68M-rnnt")

DRIVE_DIR = Path("/content/drive/MyDrive/loop-video-audio/models")
LOCAL_DIR = Path("/content/models")

def get_status():
    drive_mounted = Path("/content/drive/MyDrive").exists()
    if not drive_mounted:
        return {
            "engine": "gipformer",
            "model": model_name,
            "drive_mounted": False,
            "drive_auth_required": True,
            "model_on_drive": False,
            "model_runtime_ready": False,
            "install_status": "drive_auth_required"
        }
    
    drive_model = DRIVE_DIR / model_name
    local_model = LOCAL_DIR / model_name
    
    # Simple validation: checking if model.pt or encoder.onnx/decoder.onnx exist
    # Zipformer usually has encoder.onnx or encoder.int8.onnx
    
    def validate_model_dir(d):
        if not d.exists(): return False
        files = [f.name for f in d.iterdir()]
        has_encoder = any("encoder" in f and "onnx" in f for f in files)
        has_decoder = any("decoder" in f and "onnx" in f for f in files)
        has_tokens = "tokens.txt" in files or "bpe.model" in files
        return has_encoder and has_decoder and has_tokens
        
    on_drive = validate_model_dir(drive_model)
    on_local = validate_model_dir(local_model)
    
    return {
        "engine": "gipformer",
        "model": model_name,
        "drive_mounted": True,
        "drive_auth_required": False,
        "model_on_drive": on_drive,
        "model_runtime_ready": on_local,
        "drive_path": str(drive_model),
        "runtime_path": str(local_model),
        "install_status": "ready" if on_local or on_drive else "not_installed"
    }

def do_install():
    status = get_status()
    if status["drive_auth_required"]:
        return status
        
    drive_model = DRIVE_DIR / model_name
    partial_dir = DRIVE_DIR / (model_name + ".partial")
    
    if status["model_on_drive"]:
        return status
        
    DRIVE_DIR.mkdir(parents=True, exist_ok=True)
    
    if partial_dir.exists():
        shutil.rmtree(partial_dir)
        
    print("PROGRESS_JSON:" + json.dumps({"install_status": "Downloading model"}), flush=True)
    
    # Download from huggingface
    try:
        import huggingface_hub
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], check=True)
        import huggingface_hub
        
    try:
        if "gipformer" in model_name.lower():
            repo_id = "g-group-ai-lab/gipformer1.5-68M-rnnt"
        else:
            repo_id = "csukuangfj2/sherpa-onnx-zipformer-vi-30M-int8-2026-02-09"
            
        print("PROGRESS_JSON:" + json.dumps({"install_status": "Downloading from HuggingFace..."}), flush=True)
        huggingface_hub.snapshot_download(
            repo_id=repo_id,
            local_dir=str(partial_dir)
        )
        
        print("PROGRESS_JSON:" + json.dumps({"install_status": "Saving to Drive"}), flush=True)
        # Rename
        partial_dir.rename(drive_model)
        
        print("PROGRESS_JSON:" + json.dumps({"install_status": "Ready"}), flush=True)
    except Exception as e:
        print("PROGRESS_JSON:" + json.dumps({"install_status": "Error: " + str(e)}), flush=True)
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
            
    return get_status()

if action == "status":
    print(json.dumps(get_status()))
elif action == "install":
    print(json.dumps(do_install()))

