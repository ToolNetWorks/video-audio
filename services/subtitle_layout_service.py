import re
from PIL import ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_SIZE = 48
MAX_WIDTH = 1600

try:
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
except Exception:
    font = ImageFont.load_default()

def measure_text_width(text: str) -> int:
    return int(font.getlength(text))

def normalize_text(text: str) -> str:
    text = text.replace('\r', '').replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_punctuation_score(char: str) -> int:
    if char in ['.', '?', '!']: return 6
    if char == ';': return 5
    if char == ':': return 4
    if char == ',': return 3
    return 0

def split_text_by_width(text: str, max_width: int):
    text = normalize_text(text)
    if measure_text_width(text) <= max_width:
        return [text]
        
    fragments = []
    while text:
        if measure_text_width(text) <= max_width:
            fragments.append(text)
            break
            
        best_break = -1
        best_score = -1
        last_valid_space = -1
        
        for match in re.finditer(r'\s+', text):
            idx = match.start()
            if measure_text_width(text[:idx]) <= max_width:
                last_valid_space = idx
                prev_char = text[idx-1] if idx > 0 else ''
                score = get_punctuation_score(prev_char)
                if score >= best_score:
                    best_score = score
                    best_break = idx
            else:
                break
                
        if last_valid_space == -1:
            # A single word is too long. Force split at max_width by binary search/char.
            idx = 1
            while idx < len(text) and measure_text_width(text[:idx+1]) <= max_width:
                idx += 1
            fragments.append(text[:idx])
            text = text[idx:].strip()
        else:
            # If best_score == 0 (no punctuation), just use the longest possible segment.
            if best_score == 0:
                best_break = last_valid_space
                
            fragments.append(text[:best_break])
            text = text[best_break:].strip()
            
    return fragments

def count_chars(text: str) -> int:
    return len(text.replace(' ', ''))

def allocate_timestamps(fragments: list, start_ms: int, end_ms: int):
    if not fragments:
        return []
    if len(fragments) == 1:
        return [{"text": fragments[0], "start_ms": start_ms, "end_ms": end_ms}]
        
    total_duration = end_ms - start_ms
    weights = [count_chars(f) for f in fragments]
    total_weight = sum(weights)
    
    if total_weight == 0:
        weights = [1] * len(fragments)
        total_weight = sum(weights)
        
    durations = []
    accumulated_exact = 0.0
    for w in weights:
        exact = total_duration * w / total_weight
        durations.append(exact)
        
    # Rounding while preserving total sum
    rounded_durations = []
    error = 0.0
    for d in durations:
        val = d + error
        rounded = round(val)
        error = val - rounded
        rounded_durations.append(rounded)
        
    # Ensure sum matches exactly
    diff = sum(rounded_durations) - total_duration
    if diff != 0:
        rounded_durations[-1] -= diff
        
    result = []
    current_start = start_ms
    for i, frag in enumerate(fragments):
        dur = rounded_durations[i]
        result.append({
            "text": frag,
            "start_ms": current_start,
            "end_ms": current_start + dur
        })
        current_start += dur
        
    return result

def split_cue_to_single_line_fragments(text: str, start_ms: int, end_ms: int, source_entry_id: int):
    fragments = split_text_by_width(text, MAX_WIDTH)
    allocated = allocate_timestamps(fragments, start_ms, end_ms)
    for c in allocated:
        c["source_entry_id"] = source_entry_id
    return allocated
