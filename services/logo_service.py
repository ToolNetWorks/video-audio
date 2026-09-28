"""Logo / watermark editor service.

Storage (temporary job asset, cleaned with the job):
    /var/lib/loop-video-audio/jobs/<job_id>/logo/logo.<ext>
    /var/lib/loop-video-audio/jobs/<job_id>/logo/config.json

Coordinates are NORMALIZED (0.0-1.0), never preview pixels:
    x_norm, y_norm : top-left corner of the logo (fraction of video W/H)
    width_norm     : logo width as fraction of video width (0.03-0.40)
    opacity        : 0.0-1.0
    aspect_ratio   : original logo W/H (height derived, never stretched)

Final geometry is computed against the ACTUAL probed video dimensions,
so preview (any display size) and final render always agree.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.template_service import TemplateOverlay

LOGO_EXTENSIONS = frozenset({".png", ".webp", ".jpg", ".jpeg"})

DEFAULT_WIDTH_NORM = 0.12
MIN_WIDTH_NORM = 0.03
MAX_WIDTH_NORM = 0.40
DEFAULT_OPACITY = 0.85
SAFE_MARGIN = 0.03  # 3% canvas for presets

# Reference canvas from the spec (used only as fallback).
REF_W = 1920
REF_H = 1080


# ----------------------------------------------------------
# Paths / config
# ----------------------------------------------------------

def get_logo_dir(job_dir: Path) -> Path:
    return Path(job_dir) / "logo"


def default_config() -> dict:
    # Top-right with safe margin on the reference canvas:
    # x = 1 - margin - width, y = margin.
    # Disabled by default: jobs that never uploaded a logo must
    # render exactly like before (no LOGO_NOT_FOUND failure).
    return {
        "enabled": False,
        "x_norm": round(1.0 - SAFE_MARGIN - DEFAULT_WIDTH_NORM, 4),
        "y_norm": SAFE_MARGIN,
        "width_norm": DEFAULT_WIDTH_NORM,
        "opacity": DEFAULT_OPACITY,
        "aspect_ratio": None,
        "file_name": None,
    }


def find_logo_file(logo_dir: Path) -> Path | None:
    """Return the stored logo file, if any."""
    if not logo_dir.exists():
        return None
    for ext in LOGO_EXTENSIONS:
        for candidate in (
            logo_dir / f"logo{ext}",
            logo_dir / f"logo{ext.upper()}",
        ):
            if candidate.exists():
                return candidate
    # Fallback: any logo.* with a supported suffix.
    for child in sorted(logo_dir.iterdir()):
        if (
            child.is_file()
            and child.name.startswith("logo.")
            and child.suffix.lower() in LOGO_EXTENSIONS
        ):
            return child
    return None


def load_logo_config(job_dir: Path) -> dict:
    """Load config merged over defaults (never raises)."""
    cfg = default_config()
    path = get_logo_dir(job_dir) / "config.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(stored, dict):
        for key in cfg:
            if key in stored:
                cfg[key] = stored[key]
    return cfg


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix="logo_"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, path)


# ----------------------------------------------------------
# Validation
# ----------------------------------------------------------

def validate_logo_image(path: Path) -> tuple[int, int]:
    """Return (width, height). Raises ValueError if unreadable."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.load()
            w, h = img.size
    except Exception as exc:
        raise ValueError(f"Ảnh logo không đọc được ({exc})")
    if w <= 0 or h <= 0:
        raise ValueError("Kích thước logo không hợp lệ")
    return w, h


def clamp_config(cfg: dict) -> dict:
    """Clamp geometry into the unit canvas (guard clauses)."""
    cfg = dict(cfg)
    try:
        w = float(cfg.get("width_norm", DEFAULT_WIDTH_NORM))
    except (TypeError, ValueError):
        w = DEFAULT_WIDTH_NORM
    cfg["width_norm"] = round(
        min(MAX_WIDTH_NORM, max(MIN_WIDTH_NORM, w)), 4
    )

    try:
        op = float(cfg.get("opacity", DEFAULT_OPACITY))
    except (TypeError, ValueError):
        op = DEFAULT_OPACITY
    cfg["opacity"] = round(min(1.0, max(0.0, op)), 4)

    try:
        x = float(cfg.get("x_norm", 0.0))
    except (TypeError, ValueError):
        x = 0.0
    try:
        y = float(cfg.get("y_norm", 0.0))
    except (TypeError, ValueError):
        y = 0.0

    aspect = cfg.get("aspect_ratio")
    try:
        aspect = float(aspect) if aspect else None
    except (TypeError, ValueError):
        aspect = None
    if not aspect or aspect <= 0:
        aspect = 1.0

    # Height fraction on the reference canvas (conservative).
    h_norm = (cfg["width_norm"] * REF_W / aspect) / REF_H

    x = min(1.0, max(0.0, x))
    y = min(1.0, max(0.0, y))
    if x + cfg["width_norm"] > 1.0:
        x = 1.0 - cfg["width_norm"]
    if y + h_norm > 1.0:
        y = max(0.0, 1.0 - h_norm)

    cfg["x_norm"] = round(x, 4)
    cfg["y_norm"] = round(y, 4)
    return cfg


def save_logo_config(job_dir: Path, patch: dict) -> dict:
    """Merge patch over stored config, clamp, atomic write."""
    cfg = load_logo_config(job_dir)
    allowed = {
        "enabled", "x_norm", "y_norm", "width_norm", "opacity",
        "aspect_ratio", "file_name",
    }
    for key, value in (patch or {}).items():
        if key in allowed:
            cfg[key] = value
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg = clamp_config(cfg)
    atomic_write_json(get_logo_dir(job_dir) / "config.json", cfg)
    return cfg


# ----------------------------------------------------------
# Presets (safe margin, top-left origin)
# ----------------------------------------------------------

def preset_coords(
    name: str,
    width_norm: float,
    aspect_ratio: float | None,
    video_w: int = REF_W,
    video_h: int = REF_H,
) -> dict:
    """x_norm/y_norm for a named preset. Raises ValueError if unknown."""
    if not aspect_ratio or aspect_ratio <= 0:
        aspect_ratio = 1.0
    w = min(MAX_WIDTH_NORM, max(MIN_WIDTH_NORM, float(width_norm)))
    h_px = (w * video_w) / aspect_ratio
    h_norm = h_px / video_h
    m = SAFE_MARGIN

    presets = {
        "top-left": {"x_norm": m, "y_norm": m},
        "top-right": {"x_norm": 1.0 - m - w, "y_norm": m},
        "bottom-left": {"x_norm": m, "y_norm": 1.0 - m - h_norm},
        "bottom-right": {
            "x_norm": 1.0 - m - w,
            "y_norm": 1.0 - m - h_norm,
        },
        "center": {"x_norm": (1.0 - w) / 2.0, "y_norm": (1.0 - h_norm) / 2.0},
    }
    if name not in presets:
        raise ValueError(f"Preset không hợp lệ: {name}")
    pos = presets[name]
    cfg = {
        "x_norm": round(pos["x_norm"], 4),
        "y_norm": round(pos["y_norm"], 4),
    }
    return clamp_config({**default_config(), **cfg, "width_norm": w,
                         "aspect_ratio": aspect_ratio})


# ----------------------------------------------------------
# Final geometry (actual video dimensions)
# ----------------------------------------------------------

def probe_video_dims(path: Path) -> tuple[int, int]:
    """Return (width, height) via ffprobe. Raises RuntimeError."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "ffprobe dims failed"
        )
    parts = result.stdout.strip().split(",")
    try:
        w, h = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise RuntimeError("Không đọc được video dims")
    if w <= 0 or h <= 0:
        raise RuntimeError("Video dims không hợp lệ")
    return w, h


@dataclass
class LogoGeometry:
    x_px: int
    y_px: int
    w_px: int
    h_px: int
    opacity: float


def calculate_final_geometry(
    video_w: int,
    video_h: int,
    cfg: dict,
) -> LogoGeometry:
    """Normalized config -> pixel geometry on the real canvas."""
    cfg = clamp_config(cfg)
    aspect = cfg.get("aspect_ratio") or 1.0
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        aspect = 1.0
    if aspect <= 0:
        aspect = 1.0

    w = max(2, 2 * round((video_w * cfg["width_norm"]) / 2))
    h = max(2, 2 * round((w / aspect) / 2))
    # Logo taller than video: shrink to fit (keeps aspect).
    if h > video_h:
        h = max(2, 2 * round(video_h / 2))
        w = max(2, 2 * round((h * aspect) / 2))
    x = int(round(video_w * cfg["x_norm"]))
    y = int(round(video_h * cfg["y_norm"]))

    # Clamp inside canvas (never cut off).
    x = min(max(0, x), max(0, video_w - w))
    y = min(max(0, y), max(0, video_h - h))

    return LogoGeometry(
        x_px=x, y_px=y, w_px=w, h_px=h,
        opacity=float(cfg.get("opacity", DEFAULT_OPACITY)),
    )


# ----------------------------------------------------------
# Render resolution (enabled + valid file, or None)
# ----------------------------------------------------------

@dataclass
class RenderLogo:
    path: Path
    geometry: LogoGeometry
    file_name: str | None


def get_render_logo(job_dir: Path, video_path: Path) -> RenderLogo | None:
    """Effective logo for final render, or None when disabled.

    Raises RuntimeError("LOGO_NOT_FOUND") when enabled but the file
    is missing/invalid (loud, never silent).
    """
    job_dir = Path(job_dir)
    cfg = load_logo_config(job_dir)
    if not cfg.get("enabled"):
        return None
    logo_dir = get_logo_dir(job_dir)
    logo_path = find_logo_file(logo_dir)
    if logo_path is None:
        raise RuntimeError("LOGO_NOT_FOUND")
    try:
        video_w, video_h = probe_video_dims(video_path)
    except RuntimeError:
        video_w, video_h = REF_W, REF_H
    geometry = calculate_final_geometry(video_w, video_h, cfg)
    return RenderLogo(
        path=logo_path, geometry=geometry,
        file_name=cfg.get("file_name"),
    )


# ----------------------------------------------------------
# FFmpeg video filter (logo overlay, then optional subtitle burn)
# ----------------------------------------------------------

def build_logo_video_filter(
    *,
    logo_input_label: str,
    geometry: LogoGeometry,
    subtitle_filter: str | None = None,
    video_src_label: str = "[0:v]",
) -> tuple[str, str]:
    """Build the video filter_complex chain.

    Layering: (template ->) logo overlay -> subtitle burn, so
    subtitles always stay on top of the logo on overlap.
    video_src_label lets a template chain feed the logo.

    Returns (filter_string, output_label).
    """
    g = geometry
    chain = (
        f"[{logo_input_label}:v]"
        f"scale={g.w_px}:{g.h_px}:flags=lanczos,"
        f"format=rgba,"
        f"colorchannelmixer=aa={g.opacity:.4f}"
        f"[lg];"
        f"{video_src_label}[lg]overlay={g.x_px}:{g.y_px}:format=yuv420"
    )
    if subtitle_filter is not None:
        chain += f"[vtmp];[vtmp]{subtitle_filter}[vout]"
        return chain, "[vout]"
    chain += "[vout]"
    return chain, "[vout]"


def build_final_with_logo_command(
    *,
    base_video: Path,
    background_music: Path | None,
    logo_path: Path,
    geometry: LogoGeometry,
    output_path: Path,
    duration: float,
    base_volume: float = 100.0,
    music_volume: float = 15.0,
    subtitle_path: Path | None = None,
    start_seconds: float | None = None,
    music_start_offset: float | None = None,
    music_duration: float | None = None,
    threads: int | None = None,
    template: TemplateOverlay | None = None,
) -> list[str]:
    """One-pass final render: video + logo + subtitle + audio/music.

    Layering: base video -> template -> logo overlay -> subtitle burn.
    Inputs: 0=video, [1=music if any], logo, template(last).
    start_seconds/music_start_offset/threads behave like the
    shared audio_mix_service builders (preview support).
    """
    from services.audio_mix_service import (
        _seek_args,
        _threads_args,
        escape_subtitle_filter_path,
        music_filter_chain,
    )

    has_music = background_music is not None
    if has_music and not Path(background_music).exists():
        raise ValueError("background_music file missing for logo mix")

    logo_index = 2 if has_music else 1
    template_index = logo_index + 1

    subtitle_filter = None
    if subtitle_path is not None:
        subtitle_filter = (
            f"subtitles={escape_subtitle_filter_path(subtitle_path)}"
        )

    base_gain = base_volume / 100.0

    if has_music:
        audio_filter = music_filter_chain(
            base_volume,
            music_volume,
            music_offset=music_start_offset or 0.0,
            music_duration=music_duration,
        )
        audio_map = "[mixed]"
    elif abs(base_volume - 100.0) > 1e-9:
        audio_filter = f"[0:a:0]volume={base_gain:.4f}[aout]"
        audio_map = "[aout]"
    else:
        audio_filter = ""
        audio_map = "0:a:0?"

    video_src_label = "[0:v]"
    video_prefix = ""

    if template is not None:
        from services.template_service import build_template_chain

        tchain, _ = build_template_chain(
            input_label=str(template_index),
            overlay=template,
            src_label="[0:v]",
        )
        video_prefix = tchain + "[t];"
        video_src_label = "[t]"

    video_filter, video_out = build_logo_video_filter(
        logo_input_label=str(logo_index),
        geometry=geometry,
        subtitle_filter=subtitle_filter,
        video_src_label=video_src_label,
    )

    filter_complex = video_prefix + video_filter
    if audio_filter:
        filter_complex += f";{audio_filter}"

    command = [
        "ffmpeg",
        "-y",
    ]

    command += _seek_args(start_seconds)
    command += ["-i", str(base_video)]

    if has_music:
        command += [
            "-stream_loop", "-1",
            "-i", str(background_music),
        ]
    command += [
        "-loop", "1",
        "-i", str(logo_path),
    ]

    if template is not None:
        command += [
            "-loop", "1",
            "-i", str(template.path),
        ]

    command += [
        "-filter_complex", filter_complex,
        "-map", video_out,
        "-map", audio_map,
        "-t", f"{duration:.6f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
    ]

    if has_music or audio_map != "0:a:0?":
        command += [
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
        ]
    else:
        command += ["-c:a", "copy"]

    command += [
        "-movflags", "+faststart",
    ]

    command += _threads_args(threads)

    command += [
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]
    return command
