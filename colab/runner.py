#!/usr/bin/env python3
"""Standalone Colab full ASR runner."""
from __future__ import annotations
import json
import logging
import sys
import time
import traceback
import subprocess
import os
import re
from pathlib import Path

log = logging.getLogger("subtitle-runner")

def write_progress(stage: str, **extra):
    payload = {"stage": stage, "updated_at": time.time(), **extra}
    print(f"PROGRESS_JSON:{json.dumps(payload, ensure_ascii=False)}")
    sys.stdout.flush()

def get_audio_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    return 0.0

def download_audio(url: str, dest: Path):
    import requests
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_progress("downloading_audio", progress=0.0)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))
        downloaded = 0
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress = round((downloaded / total_size) * 100, 1)
                        write_progress("downloading_audio", progress=progress)
    if dest.stat().st_size <= 0:
        raise RuntimeError("Downloaded file empty")
    return downloaded

def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

def merge_chunks_and_create_srt(results_dir: Path, srt_path: Path):
    chunks = sorted(results_dir.glob("chunk_*.json"))
    all_segments = []
    
    for chunk_file in chunks:
        data = json.loads(chunk_file.read_text(encoding="utf-8"))
        offset = data.get("offset", 0.0)
        overlap = data.get("overlap", 0.0)
        chunk_duration = data.get("duration", 0.0)
        segments = data.get("segments", [])
        
        for seg in segments:
            start_global = offset + seg["start"]
            end_global = offset + seg["end"]
            
            # Simple deduplication: if segment start is within overlap of previous chunk
            # we just check if we already added something very similar.
            # A more robust way is just discarding segments entirely in the overlap region
            # if they duplicate the end of the previous chunk, but let's just do a basic text match
            text = seg["text"].strip()
            if not text:
                continue
                
            is_dup = False
            # Check last 5 segments for overlap
            for prev in all_segments[-5:]:
                # If times overlap and text is identical or substring
                if start_global < prev["end"] + 1.0:
                    if text in prev["text"] or prev["text"] in text:
                        is_dup = True
                        # Extend end time of previous segment
                        prev["end"] = max(prev["end"], end_global)
                        if len(text) > len(prev["text"]):
                            prev["text"] = text
                        break
            
            if not is_dup:
                all_segments.append({
                    "start": start_global,
                    "end": end_global,
                    "text": text
                })
    
    # Sort just in case
    all_segments.sort(key=lambda x: x["start"])
    
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(all_segments, 1):
            start_str = format_timestamp(seg["start"])
            end_str = format_timestamp(seg["end"])
            f.write(f"{i}\n{start_str} --> {end_str}\n{seg['text']}\n\n")
    return len(all_segments)



def run_sherpa_onnx(job_dir, input_audio, model_name, language, total_duration):
    import subprocess
    import sys
    write_progress("loading_model")
    import shutil
    
    DRIVE_DIR = Path("/content/drive/MyDrive/loop-video-audio/models") / model_name
    LOCAL_DIR = Path("/content/models") / model_name
    
    if not LOCAL_DIR.exists():
        if DRIVE_DIR.exists():
            write_progress("restoring_from_drive")
            LOCAL_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(DRIVE_DIR, LOCAL_DIR)
        else:
            raise RuntimeError(f"Model {model_name} not found in Drive. Please mount Drive and install it first.")
            
    # Install sherpa-onnx if needed
    try:
        import sherpa_onnx
    except ImportError:
        write_progress("installing_dependencies")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "sherpa-onnx"], check=True)
        import sherpa_onnx
        
    write_progress("probing_audio")
    # Convert to 16k mono wav
    wav_path = job_dir / "input_16k.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(input_audio), "-ac", "1", "-ar", "16000", str(wav_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    write_progress("transcribing", chunk=1, total_chunks=1, processed_seconds=0.0, total_duration=total_duration)
    
    import wave
    # Find model files
    encoder = list(LOCAL_DIR.glob("encoder*.onnx"))[0]
    decoder = list(LOCAL_DIR.glob("decoder*.onnx"))[0]
    joiner = list(LOCAL_DIR.glob("joiner*.onnx"))[0]
    tokens = list(LOCAL_DIR.glob("tokens.txt"))
    bpe = list(LOCAL_DIR.glob("bpe.model"))
    
    kwargs = {
        "encoder": str(encoder),
        "decoder": str(decoder),
        "joiner": str(joiner),
        "num_threads": 4
    }
    if tokens:
        kwargs["tokens"] = str(tokens[0])
    elif bpe:
        kwargs["tokens"] = str(bpe[0])
        
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
    
    with wave.open(str(wav_path), "rb") as f:
        frames = f.readframes(f.getnframes())
        import struct
        samples = struct.unpack_from(f"<{f.getnframes()}h", frames)
        samples = [s / 32768.0 for s in samples]
        
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    
    write_progress("transcribing", chunk=1, total_chunks=1, processed_seconds=total_duration, total_duration=total_duration)
    
    # Generate SRT
    tokens_res = stream.result.tokens
    timestamps = stream.result.timestamps
    words = []
    current_word = ""
    current_start = -1
    for tok, t in zip(tokens_res, timestamps):
        if tok.startswith(' ') or tok.startswith(' '):
            if current_word:
                words.append((current_word.replace(' ', '').replace(' ', ''), current_start, t))
            current_word = tok
            current_start = t
        else:
            if current_start == -1:
                current_start = t
            current_word += tok
    if current_word:
        words.append((current_word.replace(' ', '').replace(' ', ''), current_start, timestamps[-1]))

    segments = []
    current_seg = []
    for i, w in enumerate(words):
        current_seg.append(w)
        if len(current_seg) >= 12 or (i < len(words)-1 and words[i+1][1] - w[2] > 0.8):
            segments.append(current_seg)
            current_seg = []
    if current_seg:
        segments.append(current_seg)
        
    srt_path = job_dir / "subtitle.srt"
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start_str = format_timestamp(seg[0][1])
            end_str = format_timestamp(seg[-1][2] + 0.3)
            text = " ".join([w[0] for w in seg])
            f.write(f"{i}\n{start_str} --> {end_str}\n{text}\n\n")
            
    wav_path.unlink(missing_ok=True)
    write_progress("completed", srt_path=str(srt_path))


def run_whisper(job_dir, input_audio, model_name, language, total_duration, chunk_duration, overlap):
    write_progress("loading_model")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        write_progress("installing_dependencies")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "faster-whisper"], check=True)
        from faster_whisper import WhisperModel

    gpu_available = False
    try:
        import torch
        gpu_available = torch.cuda.is_available()
    except ImportError:
        pass

    device = "cuda" if gpu_available else "cpu"
    compute_type = "float16" if gpu_available else "int8"
    
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    
    results_dir = job_dir / "results"
    results_dir.mkdir(exist_ok=True)
    
    total_chunks = max(1, int((total_duration + chunk_duration - 1) // chunk_duration))
    processed_seconds = 0.0
    
    for chunk_index in range(total_chunks):
        start_time = chunk_index * chunk_duration
        end_time = min(start_time + chunk_duration, total_duration)
        if start_time >= total_duration:
            break
            
        chunk_file = job_dir / f"chunk_{chunk_index:03d}.mp3"
        result_file = results_dir / f"chunk_{chunk_index:03d}.json"
        
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                if data.get("status") == "done":
                    processed_seconds += (end_time - start_time)
                    continue
            except Exception:
                pass
        
        extract_end = min(end_time + overlap, total_duration)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", 
            "-i", str(input_audio),
            "-ss", str(start_time),
            "-to", str(extract_end),
            "-c:a", "libmp3lame", "-q:a", "2",
            str(chunk_file)
        ], check=True)
        
        write_progress("transcribing", chunk=chunk_index + 1, total_chunks=total_chunks, 
                      processed_seconds=processed_seconds, total_duration=total_duration)
                      
        segments_out = []
        segments, info = model.transcribe(
            str(chunk_file),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True
        )
        
        for seg in segments:
            segments_out.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip()
            })
            curr_processed = processed_seconds + seg.end
            write_progress("transcribing", chunk=chunk_index + 1, total_chunks=total_chunks, 
                           processed_seconds=curr_processed, total_duration=total_duration)
                           
        result_data = {
            "chunk_index": chunk_index,
            "offset": start_time,
            "overlap": overlap,
            "duration": extract_end - start_time,
            "status": "done",
            "segments": segments_out
        }
        
        tmp = result_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(result_file)
        
        chunk_file.unlink(missing_ok=True)
        processed_seconds += (end_time - start_time)
        
    write_progress("merging")
    srt_path = job_dir / "subtitle.srt"
    total_segs = merge_chunks_and_create_srt(results_dir, srt_path)
    
    write_progress("uploading_result", segments=total_segs)
    write_progress("completed", srt_path=str(srt_path))

def main():
    write_progress("starting_colab")
    
    config_data = {}
    try:
        config_text = sys.stdin.read().strip()
        if config_text:
            config_data = json.loads(config_text)
    except Exception:
        pass
        
    if not config_data:
        try:
            config_data = json.loads(Path("job.json").read_text(encoding="utf-8"))
        except Exception:
            pass

    if not config_data and "CONFIG_JSON" in globals():
        config_data = json.loads(globals()["CONFIG_JSON"])

    job_id = config_data.get("job_id", "unknown")
    audio_url = config_data.get("audio_url", "")
    model_name = config_data.get("model", "auto")
    language = config_data.get("language", "vi")
    chunk_duration = float(config_data.get("chunk_duration", 600.0))
    overlap = float(config_data.get("overlap", 2.0))

    if not audio_url:
        raise RuntimeError("No audio_url provided in config")

    job_dir = Path(f"/content/loop-video-audio/{job_id}")
    job_dir.mkdir(parents=True, exist_ok=True)
    input_audio = job_dir / "input_audio.media"
    
    download_audio(audio_url, input_audio)
    
    write_progress("probing_audio")
    total_duration = get_audio_duration(input_audio)
    if total_duration <= 0:
        raise RuntimeError("Could not determine audio duration")

    # Auto logic
    if model_name == "auto":
        if total_duration > 3600:
            model_name = "Zipformer-30M"
        else:
            model_name = "gipformer1.5-68M-rnnt"

    if "zipformer" in model_name.lower() or "gipformer" in model_name.lower():
        run_sherpa_onnx(job_dir, input_audio, model_name, language, total_duration)
    else:
        run_whisper(job_dir, input_audio, model_name, language, total_duration, chunk_duration, overlap)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_progress("failed", error=str(e), traceback=traceback.format_exc())
        sys.exit(1)
