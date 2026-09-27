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
    lines = []
    for i, e in enumerate(entries):
        lines.append(str(i + 1))
        lines.append(f"{e['start_text']} --> {e['end_text']}")
        lines.append(e['text'])
        lines.append("")
    return "\n".join(lines)

def entries_to_vtt(entries):
    from services.subtitle_layout_service import split_cue_to_single_line_fragments
    
    lines = ["WEBVTT", ""]
    
    def ms2ts(ms):
        h = ms // 3600000
        m = (ms % 3600000) // 60000
        s = (ms % 60000) // 1000
        msec = ms % 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"
        
    for e in entries:
        source_id = e['index']
        fragments = split_cue_to_single_line_fragments(e['text'], e['start_ms'], e['end_ms'], source_id)
        for frag in fragments:
            start_str = ms2ts(frag['start_ms'])
            end_str = ms2ts(frag['end_ms'])
            lines.append(str(frag['source_entry_id']))
            lines.append(f"{start_str} --> {end_str}")
            lines.append(frag['text'])
            lines.append("")
            
    return "\n".join(lines)
    

def entries_to_ass(entries):
    from services.subtitle_layout_service import split_cue_to_single_line_fragments
    
    # ASS Header with PlayResX 1920, PlayResY 1080
    # Safe area: Bottom Y = 1027, Top Y = 970 -> MarginV = 1080 - 1027 = 53
    # Wait, the subtitle should be horizontally centered.
    # We define a Style with MarginV.
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,130,130,53,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    
    def ms2ass(ms):
        h = ms // 3600000
        m = (ms % 3600000) // 60000
        s = (ms % 60000) // 1000
        msec = (ms % 1000) // 10  # 2 digits
        return f"{h}:{m:02d}:{s:02d}.{msec:02d}"
        
    lines = [header.strip()]
    for e in entries:
        source_id = e['index']
        fragments = split_cue_to_single_line_fragments(e['text'], e['start_ms'], e['end_ms'], source_id)
        for frag in fragments:
            start_str = ms2ass(frag['start_ms'])
            end_str = ms2ass(frag['end_ms'])
            # Ensure text has no newlines
            text = frag['text'].replace('\\n', ' ').replace('\\N', ' ')
            lines.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}")
            
    return "\n".join(lines)

def atomic_write(path: Path, content: str):
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix="srt_")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, path)


def get_job_dir(job_id: str) -> Path:
    from pathlib import Path
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
    
    vtt = entries_to_vtt(entries)
    atomic_write(job_dir / "preview.vtt", vtt)
    
    ass_content = entries_to_ass(entries)
    atomic_write(job_dir / "render.ass", ass_content)
    
    # Save meta
    meta = {
        "subtitle_mode": "auto" if source_type == "generated" else "upload",
        "subtitle_source": source_type,
        "subtitle_ready": True,
        "subtitle_enabled": True,
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
    clean_srt = entries_to_srt(entries)
    atomic_write(job_dir / "working.srt", clean_srt)
    vtt = entries_to_vtt(entries)
    atomic_write(job_dir / "preview.vtt", vtt)
    
    ass_content = entries_to_ass(entries)
    atomic_write(job_dir / "render.ass", ass_content)
    
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
    vtt = entries_to_vtt(entries)
    atomic_write(job_dir / "preview.vtt", vtt)
    
    ass_content = entries_to_ass(entries)
    atomic_write(job_dir / "render.ass", ass_content)
    
    if index is None:
        meta = get_meta(job_id)
        meta["edited_count"] = 0
        with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)