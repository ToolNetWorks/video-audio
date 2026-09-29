"""Single source of truth for the fixed Montserrat subtitle style.

Master files (never invent another style elsewhere):
- assets/subtitle-styles/subtitle_montserrat_white_outline.ass
  (Fontname/Fontsize/colours/outline/shadow/alignment/margins)
- assets/fonts/Montserrat-SemiBold.ttf
  (production font; also installed to fontconfig, but FFmpeg burns
  always pass fontsdir explicitly so rendering never depends on
  whatever the VPS happens to have installed)

Only the [Events] Dialogue lines are generated per job; header and
style always come from the master file. Demo Dialogue lines in the
master (if any) are never used as real subtitles.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO_ROOT / "assets" / "fonts"
MASTER_ASS_PATH = (
    REPO_ROOT / "assets" / "subtitle-styles"
    / "subtitle_montserrat_white_outline.ass"
)
MASTER_FONT_FILE = FONTS_DIR / "Montserrat-SemiBold.ttf"

_STYLE_FORMAT_FIELDS = [
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
    "OutlineColour", "BackColour", "Bold", "Italic", "Underline",
    "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle",
    "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV",
    "Encoding",
]

_master_cache: dict | None = None


def load_master_ass() -> dict:
    """Parse the master ASS into header/style/events-format parts.

    Raises RuntimeError if the master file is missing or invalid
    (loud, never silently fall back to a different style).
    """
    global _master_cache
    if _master_cache is not None:
        return _master_cache

    if not MASTER_ASS_PATH.exists():
        raise RuntimeError(
            f"ASS style master missing: {MASTER_ASS_PATH}"
        )
    if not MASTER_FONT_FILE.exists():
        raise RuntimeError(
            f"Subtitle font missing: {MASTER_FONT_FILE}"
        )

    text = MASTER_ASS_PATH.read_text(encoding="utf-8-sig")
    lines = text.replace("\r\n", "\n").split("\n")

    script_info: list[str] = []
    style_format = ""
    default_style = ""
    events_format = ""
    section = ""

    for raw in lines:
        line = raw.strip()
        if line == "[Script Info]":
            section = "info"
            continue
        if line == "[V4+ Styles]":
            section = "styles"
            continue
        if line == "[Events]":
            section = "events"
            continue
        if section == "info":
            if line:
                script_info.append(line)
        elif section == "styles":
            if line.startswith("Format:"):
                style_format = line
            elif line.startswith("Style: Default,"):
                default_style = line
        elif section == "events":
            if line.startswith("Format:"):
                events_format = line
            # Demo Dialogue lines are intentionally ignored.

    if not script_info or not style_format or not default_style:
        raise RuntimeError(
            f"ASS style master invalid (missing header/style): {MASTER_ASS_PATH}"
        )
    if not events_format:
        events_format = (
            "Format: Layer, Start, End, Style, Name, MarginL, "
            "MarginR, MarginV, Effect, Text"
        )

    fields = default_style.split("Style:", 1)[1].split(",")
    if len(fields) != len(_STYLE_FORMAT_FIELDS):
        raise RuntimeError(
            f"ASS master Default style has {len(fields)} fields, "
            f"expected {len(_STYLE_FORMAT_FIELDS)}"
        )
    style = dict(zip(_STYLE_FORMAT_FIELDS, [f.strip() for f in fields]))

    for required in ("Fontname", "Fontsize", "Alignment"):
        if not style.get(required):
            raise RuntimeError(
                f"ASS master missing required field {required}"
            )

    _master_cache = {
        "script_info": script_info,
        "style_format": style_format,
        "default_style": default_style,
        "events_format": events_format,
        "style": style,
    }
    return _master_cache


def master_style() -> dict:
    """Parsed Default style fields from the master file."""
    return load_master_ass()["style"]


def build_render_ass(dialogue_lines: list[str]) -> str:
    """Assemble render.ass: master header/style + given Dialogues."""
    master = load_master_ass()
    parts = [
        "[Script Info]",
        *master["script_info"],
        "",
        "[V4+ Styles]",
        master["style_format"],
        master["default_style"],
        "",
        "[Events]",
        master["events_format"],
    ]
    parts.extend(dialogue_lines)
    return "\n".join(parts) + "\n"


def clear_master_cache() -> None:
    """Test hook: force re-read of the master file."""
    global _master_cache
    _master_cache = None
