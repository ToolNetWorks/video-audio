import os
import re
import json
import shutil
import tempfile
from pathlib import Path
from fastapi import HTTPException, UploadFile

def parse_srt(content: str):
    # Parse SRT into structured format
    entries = []
    lines = content.strip().split('\n')
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line:
            idx += 1
            continue

        try:
            # Match number
            if not line.isdigit():
                # Maybe BOM or weird formatting
                match = re.search(r'\d+', line)
                if match:
                    index = int(match.group())
                else:
                    index = len(entries) + 1
            else:
                index = int(line)

            idx += 1
            if idx >= len(lines): break

            # Match timecode
            timecode_line = lines[idx].strip()
            time_match = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', timecode_line)
            if not time_match:
                # Try . instead of ,
                time_match = re.match(r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})', timecode_line)

            if time_match:
                start_text = time_match.group(1).replace('.', ',')
                end_text = time_match.group(2).replace('.', ',')

                def ts2ms(ts):
                    h, m, s_ms = ts.split(':')
                    s, ms = s_ms.split(',')
                    return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)

                start_ms = ts2ms(start_text)
                end_ms = ts2ms(end_text)
            else:
                # Invalid timecode, skip
                start_text = "00:00:00,000"
                end_text = "00:00:01,000"
                start_ms = 0
                end_ms = 1000

            idx += 1

            # Match text
            text_lines = []
            while idx < len(lines):
                tline = lines[idx].strip()
                if not tline:
                    break
                text_lines.append(tline)
                idx += 1

            entries.append({
                "index": len(entries) + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_text": start_text,
                "end_text": end_text,
                "text": "\n".join(text_lines),
                "edited": False
            })

        except Exception as e:
            idx += 1

    return entries

def entries_to_srt(entries):
    # Source-of-truth serialization: source cues only, never fragments.
    lines = []
    for i, e in enumerate(entries):
        lines.append(str(i + 1))
        lines.append(f"{e['start_text']} --> {e['end_text']}")
        lines.append(e['text'])
        lines.append("")
    return "\n".join(lines)

def entries_to_vtt(entries, offset_ms: int = 0):
    # Derived preview via the layout processor (single-line fragments).
    from services.subtitle_layout_service import generate_preview_vtt
    return generate_preview_vtt(entries, offset_ms=offset_ms)


def entries_to_ass(entries, offset_ms: int = 0):
    # Production burn file via the layout processor (same config).
    from services.subtitle_layout_service import generate_render_ass
    return generate_render_ass(entries, offset_ms=offset_ms)


def build_layout(entries, offset_ms: int = 0):
    from services.subtitle_layout_service import layout_entries
    return layout_entries(entries, offset_ms=offset_ms)


def get_offset_ms(job_id: str) -> int:
    """Global subtitle offset (ms). Default 0, never rewrites working.srt."""
    meta = get_meta(job_id)
    try:
        return int(meta.get("subtitle_offset_ms", 0) or 0)
    except (TypeError, ValueError):
        return 0


def set_offset_ms(job_id: str, offset_ms: int) -> int:
    """Set global offset, then regenerate derived files with it."""
    from services.subtitle_layout_service import OFFSET_MAX_MS, OFFSET_MIN_MS
    try:
        offset_ms = int(offset_ms)
    except (TypeError, ValueError):
        raise HTTPException(400, "offset_ms phải là số nguyên (ms)")
    if not (OFFSET_MIN_MS <= offset_ms <= OFFSET_MAX_MS):
        raise HTTPException(
            400,
            f"offset_ms phải trong khoảng {OFFSET_MIN_MS}..{OFFSET_MAX_MS}",
        )
    set_meta(job_id, "subtitle_offset_ms", offset_ms)
    job_dir = get_job_dir(job_id)
    regenerate_derived_files(job_dir, get_entries(job_id), offset_ms)
    return offset_ms


def regenerate_derived_files(job_dir: Path, entries, offset_ms: int = 0) -> dict:
    """Atomically rebuild preview.vtt + render.ass + layout.json.

    working.srt is the input and is never modified here.
    offset_ms shifts derived children only (uniform shift, clamp >= 0).
    """
    from services.subtitle_layout_service import (
        generate_preview_vtt,
        generate_render_ass,
        layout_entries,
    )

    layout = layout_entries(entries, offset_ms=offset_ms)
    atomic_write(
        job_dir / "preview.vtt",
        generate_preview_vtt(entries, offset_ms=offset_ms),
    )
    atomic_write(
        job_dir / "render.ass",
        generate_render_ass(entries, offset_ms=offset_ms),
    )
    atomic_write(
        job_dir / "layout.json",
        json.dumps(layout, ensure_ascii=False, indent=2),
    )
    return layout


def get_layout(job_id: str) -> dict:
    """Read cached layout; self-heal by rebuilding if missing/invalid."""
    job_dir = get_job_dir(job_id)
    layout_path = job_dir / "layout.json"
    if layout_path.exists():
        try:
            with open(layout_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    entries = get_entries(job_id)
    return regenerate_derived_files(job_dir, entries, get_offset_ms(job_id))

def atomic_write(path: Path, content: str):
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix="srt_")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, path)


def get_job_dir(job_id: str) -> Path:
    from pathlib import Path
    from services.job_lifecycle_service import validate_job_id
    validate_job_id(job_id)
    DATA_DIR = Path('/var/lib/loop-video-audio/jobs')
    return DATA_DIR / job_id / "subtitle"

def setup_subtitle_files(job_id: str, source_type: str, content: str):
    job_dir = get_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    entries = parse_srt(content)
    clean_srt = entries_to_srt(entries)

    if source_type == "generated":
        atomic_write(job_dir / "generated.srt", clean_srt)
    elif source_type == "uploaded":
        atomic_write(job_dir / "uploaded.srt", clean_srt)

    atomic_write(job_dir / "original.srt", clean_srt)
    atomic_write(job_dir / "working.srt", clean_srt)

    # Derived files (preview + burn + layout cache), atomic.
    # Fresh source resets offset to 0.
    regenerate_derived_files(job_dir, entries, 0)

    # Save meta
    meta = {
        "subtitle_mode": "auto" if source_type == "generated" else "upload",
        "subtitle_source": source_type,
        "subtitle_ready": True,
        "subtitle_enabled": True,
        "subtitle_offset_ms": 0,
        "edited_count": 0
    }
    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

def get_meta(job_id: str):
    p = get_job_dir(job_id) / "meta.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"subtitle_mode": "none", "subtitle_ready": False, "subtitle_enabled": False}

def set_meta(job_id: str, key: str, value):
    job_dir = get_job_dir(job_id)
    p = job_dir / "meta.json"
    meta = get_meta(job_id)
    meta[key] = value
    with open(p, "w", encoding="utf-8") as f:
        json.dump(meta, f)

def get_entries(job_id: str):
    p = get_job_dir(job_id) / "working.srt"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        return parse_srt(f.read())

def update_entry(job_id: str, index: int, text: str, start_ms: int = None, end_ms: int = None):
    entries = get_entries(job_id)
    if index < 1 or index > len(entries):
        raise HTTPException(400, "Invalid entry index")

    entry = entries[index - 1]
    entry["text"] = text
    entry["edited"] = True

    if start_ms is not None and end_ms is not None:
        def ms2ts(ms):
            h = ms // 3600000
            m = (ms % 3600000) // 60000
            s = (ms % 60000) // 1000
            msec = ms % 1000
            return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"
        entry["start_ms"] = start_ms
        entry["end_ms"] = end_ms
        entry["start_text"] = ms2ts(start_ms)
        entry["end_text"] = ms2ts(end_ms)

    job_dir = get_job_dir(job_id)
    # working.srt keeps SOURCE cues only; layout re-derives preview/burn
    # with the current global offset.
    clean_srt = entries_to_srt(entries)
    atomic_write(job_dir / "working.srt", clean_srt)
    regenerate_derived_files(job_dir, entries, get_offset_ms(job_id))

    meta = get_meta(job_id)
    meta["edited_count"] = meta.get("edited_count", 0) + 1
    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)

    return entry

def restore_entry(job_id: str, index: int = None):
    job_dir = get_job_dir(job_id)
    orig_path = job_dir / "original.srt"
    if not orig_path.exists():
        raise HTTPException(404, "Original SRT not found")

    with open(orig_path, "r", encoding="utf-8") as f:
        orig_entries = parse_srt(f.read())

    if index is not None:
        entries = get_entries(job_id)
        if index < 1 or index > len(entries):
            raise HTTPException(400, "Invalid entry index")
        entries[index - 1] = orig_entries[index - 1]
        # Copy over
    else:
        entries = orig_entries

    clean_srt = entries_to_srt(entries)
    atomic_write(job_dir / "working.srt", clean_srt)
    regenerate_derived_files(job_dir, entries, get_offset_ms(job_id))

    if index is None:
        meta = get_meta(job_id)
        meta["edited_count"] = 0
        with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)
