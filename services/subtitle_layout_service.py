"""Subtitle layout processor: single-line subtitles + auto time-split.

Flow:
    working.srt (source of truth, user-facing)
      -> layout processor (this module)
      -> derived cues (1 line each, never wrapped)
      -> preview.vtt (browser preview)
      -> render.ass  (final FFmpeg burn)

Rules:
- Every derived cue is exactly ONE line (no \\n, \\r, \\N).
- working.srt is NEVER rewritten with fragments.
- A long cue is split into N sequential children that exactly cover
  the original [start, end): child[0].start == original.start,
  child[-1].end == original.end, child[i].end == child[i+1].start.
- Width is measured with real font metrics (DejaVu Sans 48, the same
  font/size used by render.ass), never by character count.
- Fixed font size: long cues are SPLIT, not shrunk.
- Child durations are weighted by non-whitespace text length and
  rounded so the sum exactly equals the original duration.
- If a split child would be shorter than MIN_FRAGMENT_MS, timestamps
  are NOT stretched: the cue is flagged subtitle_too_dense instead.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


# ----------------------------------------------------------
# Style config (single source of truth for preview AND final)
# ----------------------------------------------------------

@dataclass(frozen=True)
class SubtitleStyleConfig:
    font_path: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_name: str = "DejaVu Sans"
    font_size: int = 48  # fixed; split is preferred over shrink
    canvas_width: int = 1920
    canvas_height: int = 1080
    margin_left: int = 130
    margin_right: int = 158
    margin_vertical: int = 53  # 1080 - 1027 (bottom of safe zone)
    alignment: int = 2  # bottom-center
    min_fragment_ms: int = 400

    @property
    def max_width(self) -> int:
        # 1920 - 130 - 158 = 1632
        return self.canvas_width - self.margin_left - self.margin_right


DEFAULT_CONFIG = SubtitleStyleConfig()

_font_cache: dict[tuple[str, int], object] = {}


def _get_font(config: SubtitleStyleConfig = DEFAULT_CONFIG):
    """Lazy-load the measurement font (cached)."""
    key = (config.font_path, config.font_size)
    font = _font_cache.get(key)
    if font is not None:
        return font
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(config.font_path, config.font_size)
    except Exception as exc:
        raise RuntimeError(
            "Không đo được subtitle width: "
            f"không load được font {config.font_path} ({exc})"
        )
    _font_cache[key] = font
    return font


# ----------------------------------------------------------
# Measure + normalize
# ----------------------------------------------------------

def measure_text(text: str, config: SubtitleStyleConfig = DEFAULT_CONFIG) -> float:
    """Actual rendered width in pixels with the production font/size."""
    if not text:
        return 0.0
    return float(_get_font(config).getlength(text))


def measure_text_width(text: str) -> int:
    """Backward-compatible int wrapper."""
    return int(measure_text(text))


def normalize_single_line(text: str) -> str:
    """Collapse multiline SRT input into one display line."""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("\\N", " ").replace("\\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(text: str) -> str:
    """Backward-compatible alias."""
    return normalize_single_line(text)


def _weight(text: str) -> int:
    """Text weight = chars ignoring whitespace (spec section 14)."""
    return len(re.sub(r"\s+", "", text))


def count_chars(text: str) -> int:
    """Backward-compatible fixed version (whitespace excluded)."""
    return _weight(text or "")


# ----------------------------------------------------------
# Split points (semantic priority)
# ----------------------------------------------------------

# Break priority: . ? !  >  ;  >  :  >  ,  >  plain space.
_PUNCT_SCORE = {
    ".": 6, "?": 6, "!": 6,
    ";": 5,
    ":": 4,
    ",": 3,
}


def get_punctuation_score(char: str) -> int:
    return _PUNCT_SCORE.get(char or "", 0)


def find_split_points(text: str, config: SubtitleStyleConfig = DEFAULT_CONFIG) -> list[dict]:
    """All whitespace positions where the prefix fits max_width.

    Each point: {"index": int, "score": int}. Ordered left -> right.
    """
    text = normalize_single_line(text)
    if not text:
        return []
    points = []
    for match in re.finditer(r"\s+", text):
        idx = match.start()
        if measure_text(text[:idx], config) > config.max_width:
            break
        prev = text[idx - 1] if idx > 0 else ""
        points.append({"index": idx, "score": get_punctuation_score(prev)})
    return points


def _hard_cut(text: str, config: SubtitleStyleConfig) -> int:
    """Binary-search char index for an over-long single word."""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure_text(text[:mid], config) <= config.max_width:
            lo = mid
        else:
            hi = mid - 1
    return max(1, lo)


def split_text_to_fragments(
    text: str,
    max_width: int | None = None,
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
) -> list[str]:
    """Split into the fewest single-line fragments that fit max_width.

    Greedy: at each step take the longest fitting prefix, preferring
    the highest-priority semantic break (ties -> rightmost). Never
    cuts inside a word unless a single word alone overflows.
    """
    text = normalize_single_line(text)
    if not text:
        return []
    limit = max_width if max_width is not None else config.max_width
    if measure_text(text, config) <= limit:
        return [text]

    fragments: list[str] = []
    rest = text
    while rest:
        if measure_text(rest, config) <= limit:
            fragments.append(rest)
            break

        best_break = -1
        best_score = -1
        last_valid = -1
        for match in re.finditer(r"\s+", rest):
            idx = match.start()
            if measure_text(rest[:idx], config) > limit:
                break
            last_valid = idx
            prev = rest[idx - 1] if idx > 0 else ""
            score = get_punctuation_score(prev)
            if score >= best_score:
                best_score = score
                best_break = idx

        if last_valid == -1:
            cut = _hard_cut(rest, config)
            fragments.append(rest[:cut])
            rest = rest[cut:].strip()
            continue

        if best_score <= 0:
            best_break = last_valid
        head = rest[:best_break].strip()
        if head:
            fragments.append(head)
        rest = rest[best_break:].strip()
        if not rest:
            break

    return [f for f in (normalize_single_line(f) for f in fragments) if f]


def split_text_by_width(text: str, max_width: int):
    """Backward-compatible alias."""
    return split_text_to_fragments(text, max_width=max_width)


# ----------------------------------------------------------
# Timestamp allocation (weight-based, sum-preserving)
# ----------------------------------------------------------

def allocate_fragment_timestamps(
    fragments: list[str],
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Split [start_ms, end_ms) across fragments by text weight.

    Guarantees: out[0].start == start_ms, out[-1].end == end_ms,
    out[i].end == out[i+1].start, sum(durations) == total.
    """
    if not fragments:
        return []
    if len(fragments) == 1:
        return [{
            "text": fragments[0],
            "start_ms": start_ms,
            "end_ms": max(end_ms, start_ms),
        }]

    total = max(0, end_ms - start_ms)
    weights = [_weight(f) for f in fragments]
    if sum(weights) <= 0:
        weights = [1] * len(fragments)
    total_weight = sum(weights)

    # Error-diffusion rounding so the rounded parts sum to total.
    durations: list[int] = []
    error = 0.0
    for w in weights:
        exact = total * w / total_weight + error
        rounded = int(round(exact))
        error = exact - rounded
        durations.append(max(0, rounded))
    durations[-1] += total - sum(durations)
    if durations[-1] < 0:
        durations[-1] = 0

    out: list[dict] = []
    cursor = start_ms
    for frag, dur in zip(fragments, durations):
        out.append({"text": frag, "start_ms": cursor, "end_ms": cursor + dur})
        cursor += dur
    return out


def allocate_timestamps(fragments: list, start_ms: int, end_ms: int):
    """Backward-compatible alias."""
    return allocate_fragment_timestamps(fragments, start_ms, end_ms)


def split_cue_to_single_line_fragments(
    text: str,
    start_ms: int,
    end_ms: int,
    source_entry_id: int,
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
):
    """Backward-compatible: derived children with source_entry_id."""
    fragments = split_text_to_fragments(text, config=config)
    allocated = allocate_fragment_timestamps(fragments, start_ms, end_ms)
    for i, child in enumerate(allocated):
        child["source_entry_id"] = source_entry_id
        child["fragment_index"] = i + 1
        child["fragment_count"] = len(allocated)
    return allocated


# ----------------------------------------------------------
# Global offset (applied AFTER split, never rewrites source)
# ----------------------------------------------------------

OFFSET_MIN_MS = -60000
OFFSET_MAX_MS = 60000


def apply_offset_to_children(children: list[dict], offset_ms: int) -> list[dict]:
    """Shift derived children by a global offset.

    Uniform shift preserves continuity (end == next start).
    Values pushed below zero are clamped to zero (no negative
    timestamps); working.srt is never touched.
    """
    if not children:
        return children
    if not offset_ms:
        return children
    shifted = [
        {
            **c,
            "start_ms": c["start_ms"] + offset_ms,
            "end_ms": c["end_ms"] + offset_ms,
        }
        for c in children
    ]
    for c in shifted:
        if c["start_ms"] < 0:
            c["start_ms"] = 0
        if c["end_ms"] < 0:
            c["end_ms"] = 0
    return shifted


# ----------------------------------------------------------
# Full entry layout (+ dense detection, no timestamp stretching)
# ----------------------------------------------------------

def layout_entries(
    entries: list[dict],
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
    offset_ms: int = 0,
) -> dict:
    """Layout every source entry. Returns derived cues + dense flags.

    offset_ms is applied to derived children AFTER split (uniform
    shift, clamped at zero). Source timestamps are never modified.
    """
    offset_ms = int(offset_ms or 0)
    cues: list[dict] = []
    dense_ids: list[int] = []
    for entry in entries or []:
        source_id = entry.get("index")
        start_ms = int(entry.get("start_ms", 0))
        end_ms = int(entry.get("end_ms", 0))
        if end_ms < start_ms:
            end_ms = start_ms
        children = split_cue_to_single_line_fragments(
            entry.get("text", ""), start_ms, end_ms, source_id, config,
        )
        children = apply_offset_to_children(children, offset_ms)
        too_dense = (
            len(children) > 1
            and any(
                (c["end_ms"] - c["start_ms"]) < config.min_fragment_ms
                for c in children
            )
        )
        if too_dense:
            dense_ids.append(source_id)
        cues.append({
            "source_entry_id": source_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "fragment_count": len(children),
            "too_dense": too_dense,
            "fragments": children,
        })
    return {
        "version": 1,
        "offset_ms": offset_ms,
        "config": {
            "font_name": config.font_name,
            "font_size": config.font_size,
            "canvas_width": config.canvas_width,
            "canvas_height": config.canvas_height,
            "margin_left": config.margin_left,
            "margin_right": config.margin_right,
            "margin_vertical": config.margin_vertical,
            "max_width": config.max_width,
            "min_fragment_ms": config.min_fragment_ms,
        },
        "cues": cues,
        "dense_source_ids": dense_ids,
        "subtitle_too_dense": len(dense_ids) > 0,
    }


# ----------------------------------------------------------
# Derived file generators (preview.vtt + render.ass)
# ----------------------------------------------------------

def _ms_to_vtt(ms: int) -> str:
    ms = max(0, int(ms))
    return (
        f"{ms // 3600000:02d}:{(ms % 3600000) // 60000:02d}:"
        f"{(ms % 60000) // 1000:02d}.{ms % 1000:03d}"
    )


def _ms_to_ass(ms: int) -> str:
    ms = max(0, int(ms))
    return (
        f"{ms // 3600000}:{(ms % 3600000) // 60000:02d}:"
        f"{(ms % 60000) // 1000:02d}.{(ms % 1000) // 10:02d}"
    )


def _cue_id(source_id, fragment_index: int, fragment_count: int) -> str:
    if fragment_count <= 1:
        return str(source_id)
    return f"{source_id}.{fragment_index}"


def generate_preview_vtt(
    entries: list[dict],
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
    offset_ms: int = 0,
) -> str:
    """preview.vtt with derived single-line cues (no FFmpeg needed)."""
    layout = layout_entries(entries, config, offset_ms)
    lines = ["WEBVTT", ""]
    for cue in layout["cues"]:
        for frag in cue["fragments"]:
            lines.append(_cue_id(
                frag["source_entry_id"],
                frag["fragment_index"],
                frag["fragment_count"],
            ))
            lines.append(
                f"{_ms_to_vtt(frag['start_ms'])} --> "
                f"{_ms_to_vtt(frag['end_ms'])}"
            )
            lines.append(frag["text"])
            lines.append("")
    return "\n".join(lines)


def render_ass_string(
    fragments: list[dict],
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
) -> str:
    """Render an ASS document from flat fragment dicts.

    Each fragment needs start_ms/end_ms/text. Shared style with
    generate_render_ass so preview and final always match.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {config.canvas_width}\n"
        f"PlayResY: {config.canvas_height}\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{config.font_name},{config.font_size},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,"
        f"{config.margin_left},{config.margin_right},{config.margin_vertical},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text"
    )
    lines = [header]
    for frag in fragments or []:
        # Fragments are single-line by construction; strip any
        # stray newline markers defensively (never hide content).
        text = normalize_single_line(frag.get("text", ""))
        lines.append(
            f"Dialogue: 0,{_ms_to_ass(frag['start_ms'])},"
            f"{_ms_to_ass(frag['end_ms'])},"
            f"Default,,0,0,0,,{text}"
        )
    return "\n".join(lines)


def generate_render_ass(
    entries: list[dict],
    config: SubtitleStyleConfig = DEFAULT_CONFIG,
    offset_ms: int = 0,
) -> str:
    """render.ass: 1 event = 1 fragment, no newline, no auto-wrap."""
    layout = layout_entries(entries, config, offset_ms)
    flat = [
        frag
        for cue in layout["cues"]
        for frag in cue["fragments"]
    ]
    return render_ass_string(flat, config)
