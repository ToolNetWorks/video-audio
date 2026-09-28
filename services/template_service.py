"""Template / frame overlay service.

Modes: "system" | "upload" | "none" (default: system).

Storage:
    System (read-only, never cleaned, never copied into jobs):
        /root/loop-video-audio/assets/templates/<id>.svg|png|webp
    Job (temporary, cleaned with the job):
        /var/lib/loop-video-audio/jobs/<job_id>/template/source.<ext>
        /var/lib/loop-video-audio/jobs/<job_id>/template/render.png
        /var/lib/loop-video-audio/jobs/<job_id>/template/config.json

Unlike logos, templates always cover the full canvas (1920x1080
reference, scaled to the real video size). No drag, no resize.

SVG safety: uploaded SVGs are XML-parsed, stripped of
script/foreignObject/event-handlers/external references, then
rasterized server-side (cairosvg, ImageMagick fallback). Raw user
SVG is never trusted in the render path or the preview DOM.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_MODES = frozenset({"system", "upload", "none"})

TEMPLATE_EXTENSIONS = frozenset({".svg", ".png", ".webp"})

SYSTEM_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "assets" / "templates"
)

DEFAULT_SYSTEM_TEMPLATE = "default"

# Reference canvas. Final geometry scales to the real video size.
REF_W = 1920
REF_H = 1080


# ----------------------------------------------------------
# System templates
# ----------------------------------------------------------

def _system_type(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "png"


def _system_name(tid: str) -> str:
    names = {
        "default": "Mặc định",
    }
    return names.get(tid, tid.replace("-", " ").replace("_", " ").title())


def list_system_templates() -> list[dict]:
    """Scan assets dir (allowlist for API exposure)."""
    out: list[dict] = []
    if not SYSTEM_TEMPLATES_DIR.exists():
        return out
    for child in sorted(SYSTEM_TEMPLATES_DIR.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower() not in TEMPLATE_EXTENSIONS:
            continue
        tid = child.stem
        out.append({
            "id": tid,
            "name": _system_name(tid),
            "type": _system_type(child),
            "preview_url": f"/api/templates/{tid}/preview",
            "asset_url": f"/api/templates/{tid}/asset",
        })
    return out


def get_system_template_path(template_id: str) -> Path | None:
    """Resolve a known template id. None when unknown (no traversal)."""
    if not template_id or not re.fullmatch(r"[A-Za-z0-9_-]+", template_id):
        return None
    for child in [SYSTEM_TEMPLATES_DIR / f"{template_id}{ext}"
                  for ext in (".svg", ".png", ".webp")]:
        if child.is_file():
            return child
    return None


# ----------------------------------------------------------
# Config
# ----------------------------------------------------------

def get_template_dir(job_dir: Path) -> Path:
    return Path(job_dir) / "template"


def default_config() -> dict:
    return {
        "mode": "system",
        "enabled": True,
        "system_template": DEFAULT_SYSTEM_TEMPLATE,
        "uploaded_file": None,
        "opacity": 1.0,
    }


def load_template_config(job_dir: Path) -> dict:
    """Load config merged over defaults (never raises)."""
    cfg = default_config()
    path = get_template_dir(job_dir) / "config.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(stored, dict):
        for key in cfg:
            if key in stored:
                cfg[key] = stored[key]
    # Keep mode/enabled consistent: none means disabled.
    if cfg.get("mode") not in TEMPLATE_MODES:
        cfg["mode"] = "system"
    cfg["enabled"] = cfg["mode"] != "none"
    try:
        cfg["opacity"] = float(cfg.get("opacity", 1.0))
    except (TypeError, ValueError):
        cfg["opacity"] = 1.0
    return cfg


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix="tpl_"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, path)


def _validate_opacity(value) -> float:
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        raise ValueError("INVALID_TEMPLATE_MODE: opacity phải là số")
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("INVALID_TEMPLATE_MODE: opacity phải 0..1")
    return round(opacity, 4)


def save_template_config(job_dir: Path, patch: dict) -> dict:
    """Merge patch, validate, atomic write. Raises ValueError."""
    cfg = load_template_config(job_dir)
    patch = patch or {}

    if "mode" in patch:
        mode = patch["mode"]
        if mode not in TEMPLATE_MODES:
            raise ValueError("INVALID_TEMPLATE_MODE")
        cfg["mode"] = mode

    if "system_template" in patch:
        tid = patch["system_template"]
        if get_system_template_path(tid) is None:
            raise ValueError("TEMPLATE_NOT_FOUND")
        cfg["system_template"] = tid

    if "opacity" in patch:
        cfg["opacity"] = _validate_opacity(patch["opacity"])

    if cfg["mode"] == "upload":
        if find_upload_source(get_template_dir(job_dir)) is None:
            raise ValueError("TEMPLATE_UPLOAD_REQUIRED")

    cfg["enabled"] = cfg["mode"] != "none"
    atomic_write_json(get_template_dir(job_dir) / "config.json", cfg)
    return cfg


def reset_template_config(job_dir: Path) -> dict:
    """Reset to system/default/100%. Keeps any uploaded file."""
    cfg = load_template_config(job_dir)
    cfg["mode"] = "system"
    cfg["system_template"] = DEFAULT_SYSTEM_TEMPLATE
    cfg["opacity"] = 1.0
    cfg["enabled"] = True
    atomic_write_json(get_template_dir(job_dir) / "config.json", cfg)
    return cfg


# ----------------------------------------------------------
# Upload validation
# ----------------------------------------------------------

def validate_template_upload(filename: str | None, path: Path) -> str:
    """Return normalized extension. Raises ValueError on problems."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in TEMPLATE_EXTENSIONS:
        raise ValueError("INVALID_TEMPLATE_FILE: chỉ nhận .svg/.png/.webp")
    try:
        size = path.stat().st_size
    except OSError:
        raise ValueError("INVALID_TEMPLATE_FILE: không đọc được file")
    if size <= 0:
        raise ValueError("INVALID_TEMPLATE_FILE: file rỗng")

    if suffix == ".svg":
        try:
            raw = path.read_bytes()
        except OSError:
            raise ValueError("INVALID_TEMPLATE_FILE: không đọc được file")
        sanitize_svg(raw)  # raises on invalid/unsafe
    else:
        try:
            from PIL import Image
            with Image.open(path) as img:
                img.load()
                if img.size[0] <= 0 or img.size[1] <= 0:
                    raise ValueError("INVALID_TEMPLATE_FILE")
        except ValueError:
            raise
        except Exception:
            raise ValueError("INVALID_TEMPLATE_FILE: ảnh corrupt")
    return suffix


def find_upload_source(template_dir: Path) -> Path | None:
    """Internal stored upload (source.svg/png/webp)."""
    template_dir = Path(template_dir)
    if not template_dir.exists():
        return None
    for ext in (".svg", ".png", ".webp"):
        candidate = template_dir / f"source{ext}"
        if candidate.is_file():
            return candidate
    return None


def store_upload(template_dir: Path, src: Path, ext: str) -> Path:
    """Move validated upload to internal name; invalidate render."""
    template_dir = Path(template_dir)
    template_dir.mkdir(parents=True, exist_ok=True)
    for old in template_dir.glob("source.*"):
        if old.is_file():
            old.unlink(missing_ok=True)
    dest = template_dir / f"source{ext}"
    src.replace(dest)
    render = template_dir / "render.png"
    render.unlink(missing_ok=True)
    return dest


def delete_upload(job_dir: Path) -> dict:
    """Remove uploaded source + derived render. Falls back to system."""
    template_dir = get_template_dir(job_dir)
    for name in ("source.svg", "source.png", "source.webp", "render.png"):
        try:
            (template_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    cfg = load_template_config(job_dir)
    if cfg.get("mode") == "upload":
        cfg["mode"] = "system"
        cfg["enabled"] = True
        cfg["uploaded_file"] = None
        atomic_write_json(template_dir / "config.json", cfg)
    return cfg


# ----------------------------------------------------------
# SVG security + rasterization
# ----------------------------------------------------------

_EVENT_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)
_UNSAFE_URL_RE = re.compile(
    r"^\s*(javascript:|data:(?!image/(png|jpeg|gif|webp);base64,)|https?:|//)",
    re.IGNORECASE,
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def sanitize_svg(raw: bytes) -> bytes:
    """Strip active/external content. Raises ValueError if unsafe/invalid."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("INVALID_TEMPLATE_FILE: SVG không phải UTF-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ValueError("INVALID_TEMPLATE_FILE: SVG không hợp lệ")

    if _local_name(root.tag) != "svg":
        raise ValueError("INVALID_TEMPLATE_FILE: không phải SVG")

    for elem in list(root.iter()):
        name = _local_name(elem.tag)
        if name in ("script", "foreignobject"):
            raise ValueError("UNSAFE_SVG: chứa script/foreignObject")
        for attr in list(elem.attrib):
            lname = attr.rsplit("}", 1)[-1]
            if _EVENT_ATTR_RE.match(lname):
                del elem.attrib[attr]
                continue
            if lname.lower() in ("href", "src", "action", "formaction",
                                 "xlink:href", "data"):
                value = str(elem.attrib[attr])
                if _UNSAFE_URL_RE.match(value) or "expression(" in value.lower():
                    del elem.attrib[attr]
                    continue
            if lname.lower() == "style":
                style = str(elem.attrib[attr]).lower()
                if ("javascript:" in style or "expression(" in style
                        or "url(http" in style or "url(//" in style):
                    del elem.attrib[attr]

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def rasterize_svg(
    svg_bytes: bytes,
    out_png: Path,
    width: int = REF_W,
    height: int = REF_H,
) -> Path:
    """Render SVG to RGBA PNG (cairosvg, ImageMagick fallback)."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    try:
        import cairosvg
        cairosvg.svg2png(
            bytestring=svg_bytes,
            write_to=str(out_png),
            output_width=int(width),
            output_height=int(height),
        )
    except Exception:
        # Fallback: ImageMagick MSVG renderer.
        tmp_svg = out_png.with_suffix(".san.svg")
        try:
            tmp_svg.write_bytes(svg_bytes)
            result = subprocess.run(
                ["convert", "-background", "none", str(tmp_svg),
                 "-resize", f"{int(width)}x{int(height)}!",
                 str(out_png)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0 or not out_png.exists():
                raise RuntimeError("TEMPLATE_RENDER_FAILED")
        finally:
            try:
                tmp_svg.unlink(missing_ok=True)
            except OSError:
                pass

    # Normalize to RGBA regardless of backend.
    try:
        from PIL import Image
        with Image.open(out_png) as img:
            rgba = img.convert("RGBA")
            if rgba.size != (int(width), int(height)):
                rgba = rgba.resize((int(width), int(height)))
            rgba.save(out_png)
    except Exception:
        raise RuntimeError("TEMPLATE_RENDER_FAILED")
    return out_png


def ensure_raster(template_dir: Path) -> Path:
    """Return 1920x1080 RGBA render.png for the uploaded SVG.

    Reuses the cache when the source is unchanged.
    """
    template_dir = Path(template_dir)
    source = find_upload_source(template_dir)
    if source is None or source.suffix.lower() != ".svg":
        raise ValueError("TEMPLATE_NOT_FOUND")
    render = template_dir / "render.png"
    try:
        if (render.exists()
                and render.stat().st_mtime >= source.stat().st_mtime
                and render.stat().st_size > 0):
            return render
    except OSError:
        pass
    raw = source.read_bytes()
    clean = sanitize_svg(raw)
    return rasterize_svg(clean, render)


# ----------------------------------------------------------
# Render resolution
# ----------------------------------------------------------

@dataclass
class TemplateOverlay:
    path: Path        # render-ready PNG (system asset or cache)
    opacity: float
    width: int        # real video width
    height: int       # real video height


def probe_video_dims(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
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
        raise RuntimeError("ffprobe dims failed")
    parts = result.stdout.strip().split(",")
    try:
        w, h = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise RuntimeError("Không đọc được video dims")
    if w <= 0 or h <= 0:
        raise RuntimeError("Video dims không hợp lệ")
    return w, h


def _raster_overlay(path: Path, opacity: float,
                    video_w: int, video_h: int) -> TemplateOverlay:
    return TemplateOverlay(
        path=Path(path),
        opacity=round(float(opacity), 4),
        width=int(video_w),
        height=int(video_h),
    )


def resolve_template(job_dir: Path, video_path: Path) -> TemplateOverlay | None:
    """Effective template for render, or None when mode is none.

    Raises RuntimeError with a clear code (TEMPLATE_UPLOAD_REQUIRED /
    TEMPLATE_NOT_FOUND / INVALID_TEMPLATE_FILE / UNSAFE_SVG /
    TEMPLATE_RENDER_FAILED). Never falls back silently.
    """
    job_dir = Path(job_dir)
    cfg = load_template_config(job_dir)
    mode = cfg.get("mode", "system")

    if mode == "none":
        return None

    try:
        video_w, video_h = probe_video_dims(video_path)
    except RuntimeError:
        video_w, video_h = REF_W, REF_H

    opacity = cfg.get("opacity", 1.0)
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        opacity = 1.0

    if mode == "system":
        tid = cfg.get("system_template") or DEFAULT_SYSTEM_TEMPLATE
        asset = get_system_template_path(tid)
        if asset is None:
            raise RuntimeError("TEMPLATE_NOT_FOUND")
        render_path = asset
        if asset.suffix.lower() == ".svg":
            # Cache raster next to the asset dir? No: system assets
            # are read-only. Rasterize to a shared cache dir.
            cache_dir = Path("/tmp/loop-video-audio-template-cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{tid}.png"
            try:
                if not (cached.exists()
                        and cached.stat().st_mtime >= asset.stat().st_mtime):
                    rasterize_svg(asset.read_bytes(), cached)
            except Exception as exc:
                raise RuntimeError(f"TEMPLATE_RENDER_FAILED: {exc}")
            render_path = cached
        return _raster_overlay(render_path, opacity, video_w, video_h)

    # mode == "upload"
    template_dir = get_template_dir(job_dir)
    source = find_upload_source(template_dir)
    if source is None:
        raise RuntimeError("TEMPLATE_UPLOAD_REQUIRED")
    ext = source.suffix.lower()
    try:
        if ext == ".svg":
            render_path = ensure_raster(template_dir)
        else:
            from PIL import Image
            with Image.open(source) as img:
                img.load()
            render_path = source
    except RuntimeError:
        raise
    except ValueError as exc:
        raise RuntimeError(str(exc))
    except Exception:
        raise RuntimeError("INVALID_TEMPLATE_FILE")
    return _raster_overlay(render_path, opacity, video_w, video_h)


# ----------------------------------------------------------
# Shared video filter (template FIRST, then logo, then subtitle)
# ----------------------------------------------------------

def build_template_chain(
    *,
    input_label: str,
    overlay: TemplateOverlay,
    src_label: str = "[0:v]",
) -> tuple[str, str]:
    """Full-canvas template overlay.

    Returns (filter_string, output_label). The template is scaled
    exactly to the video canvas (no manual geometry).
    """
    chain = (
        f"[{input_label}:v]"
        f"scale={overlay.width}:{overlay.height}:flags=lanczos,"
        f"format=rgba,"
        f"colorchannelmixer=aa={overlay.opacity:.4f}"
        f"[tf];"
        f"{src_label}[tf]overlay=0:0:format=yuv420"
    )
    return chain, "[tout]"
