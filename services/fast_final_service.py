"""Fast-path final render: avoid re-encoding a long video for static overlays.

Three internal modes (music never decides the video encode mode):

- FAST_COPY: no logo, no template, no subtitle burn.
  Video is stream-copied even with background music / volume changes
  (only audio is encoded when an audio filter is needed).

- DECORATED_LOOP_COPY: static logo and/or template, NO subtitle burn.
  The short upload source (kept in the job dir by V1) is decorated
  ONCE, then looped with ``-stream_loop`` + ``-c:v copy`` to the full
  final duration. Voice audio is taken from the long V1 base video so
  audio stays bit-identical to the full-encode path.

- FULL_ENCODE: subtitle burn-in (timeline-dependent) or fast path
  unavailable/failed. Legacy one-pass encode of the whole duration.

Layering parity with the legacy pipeline is guaranteed by reusing the
exact same filter builders (build_template_chain + build_logo_video_filter)
with the exact same geometry: base video -> template -> logo -> subtitle.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.logo_service import LogoGeometry, RenderLogo
    from services.template_service import TemplateOverlay

# Internal render modes.
FAST_COPY = "FAST_COPY"
DECORATED_LOOP_COPY = "DECORATED_LOOP_COPY"
FULL_ENCODE = "FULL_ENCODE"

# Must stay in sync with the legacy full-encode output.
VIDEO_CODEC_ARGS = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
]
AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]

CACHE_VIDEO_NAME = "decorated-loop.mp4"
CACHE_META_NAME = "decorated-loop.json"

# Files larger than this use size+mtime fingerprinting instead of sha1.
_SHA1_SIZE_LIMIT = 64 * 1024 * 1024


def decide_final_render_mode(
    *,
    has_logo: bool,
    has_template: bool,
    has_subtitle: bool,
) -> str:
    """Pick the internal render mode.

    Subtitle burn-in is timeline-dependent, so it always forces the
    full one-pass encode. Music never influences the video mode.
    """
    if has_subtitle:
        return FULL_ENCODE
    if has_logo or has_template:
        return DECORATED_LOOP_COPY
    return FAST_COPY


def get_short_source(job_dir: Path) -> tuple[Path | None, float | None]:
    """Return the short upload source kept by V1, or (None, None).

    Reads ``state.json`` (``video_path``) written by process_v1 and
    probes its duration. Never raises: a missing source simply means
    the caller must fall back to FULL_ENCODE.
    """
    try:
        state_path = Path(job_dir) / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        video_path = Path(state["video_path"])
        if not video_path.exists():
            return None, None
    except (OSError, ValueError, KeyError):
        return None, None
    try:
        import subprocess

        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True,
        )
        duration = float(result.stdout.strip())
        if duration <= 0:
            return None, None
        return video_path, duration
    except (OSError, ValueError):
        return None, None


def file_fingerprint(path: Path) -> str:
    """Stable fingerprint for cache keys (sha1 for small files)."""
    stat = path.stat()
    if stat.st_size > _SHA1_SIZE_LIMIT:
        return f"stat:{stat.st_size}:{stat.st_mtime_ns}"
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return f"sha1:{digest.hexdigest()}"


def decorated_loop_cache_key(
    *,
    source_video: Path,
    logo_path: Path | None,
    geometry=None,
    template=None,
    video_width: int,
    video_height: int,
) -> str:
    """Hash the full decorated-loop config.

    Any change (source, logo file/geometry, template file/opacity,
    canvas size, codec settings) produces a different key so a stale
    cached loop is never reused.
    """
    payload = {
        "source": file_fingerprint(source_video),
        "logo": file_fingerprint(logo_path) if logo_path is not None else None,
        "geometry": (
            {
                "x": geometry.x_px,
                "y": geometry.y_px,
                "w": geometry.w_px,
                "h": geometry.h_px,
                "opacity": geometry.opacity,
            }
            if geometry is not None
            else None
        ),
        "template": (
            {
                "file": file_fingerprint(Path(template.path)),
                "opacity": template.opacity,
                "width": template.width,
                "height": template.height,
            }
            if template is not None
            else None
        ),
        "canvas": [video_width, video_height],
        "codec": VIDEO_CODEC_ARGS,
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def cache_paths(job_dir: Path) -> tuple[Path, Path]:
    cache_dir = Path(job_dir) / "cache"
    return cache_dir / CACHE_VIDEO_NAME, cache_dir / CACHE_META_NAME


def read_decorated_cache(job_dir: Path, key: str) -> Path | None:
    """Return the cached decorated loop if the key matches, else None."""
    video_path, meta_path = cache_paths(job_dir)
    try:
        if not video_path.exists() or video_path.stat().st_size == 0:
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("key") != key:
        return None
    return video_path


def write_decorated_cache(
    job_dir: Path,
    key: str,
    *,
    source_duration: float,
) -> None:
    video_path, meta_path = cache_paths(job_dir)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "key": key,
                "source_duration": source_duration,
                "created_at": time.time(),
            },
            indent=1,
        ),
        encoding="utf-8",
    )


def build_decorated_loop_command(
    *,
    source_video: Path,
    source_duration: float,
    logo_path: Path | None,
    geometry=None,
    template=None,
    output_path: Path,
    threads: int | None = None,
) -> list[str]:
    """Encode logo/template ONCE on the short source (no audio kept).

    Uses the exact same filter builders as the legacy full-encode path
    so pixels are identical; only the input (short vs long video) and
    the output length change.
    """
    from services.logo_service import build_logo_video_filter
    from services.audio_mix_service import _threads_args

    if logo_path is None and template is None:
        raise ValueError("decorated loop needs a logo and/or template")

    # Input layout: 0=source video, 1=logo (if any), last=template (if any).
    next_index = 1
    logo_index = None
    if logo_path is not None:
        logo_index = next_index
        next_index += 1
    template_index = None
    if template is not None:
        template_index = next_index
        next_index += 1

    video_src_label = "[0:v]"
    tchain = ""
    if template is not None:
        from services.template_service import build_template_chain

        tchain, _ = build_template_chain(
            input_label=str(template_index),
            overlay=template,
            src_label="[0:v]",
        )

    if logo_path is not None:
        video_prefix = tchain + "[t];" if template is not None else ""
        video_src_label = "[t]" if template is not None else "[0:v]"
        video_filter, video_out = build_logo_video_filter(
            logo_input_label=str(logo_index),
            geometry=geometry,
            subtitle_filter=None,
            video_src_label=video_src_label,
        )
        filter_complex = video_prefix + video_filter
    else:
        # Template-only: same chain shape as the legacy no-music/
        # with-music builders (tchain + "[tout]", mapped as [tout]).
        filter_complex = tchain + "[tout]"
        video_out = "[tout]"

    command = ["ffmpeg", "-y", "-i", str(source_video)]
    if logo_path is not None:
        command += ["-loop", "1", "-i", str(logo_path)]
    if template is not None:
        command += ["-loop", "1", "-i", str(template.path)]
    command += [
        "-filter_complex", filter_complex,
        "-map", video_out,
        "-t", f"{source_duration:.6f}",
        "-an",
        *VIDEO_CODEC_ARGS,
        "-movflags", "+faststart",
    ]
    command += _threads_args(threads)
    command += ["-progress", "pipe:1", "-nostats", str(output_path)]
    return command


def build_loop_copy_final_command(
    *,
    loop_video: Path,
    base_video: Path,
    background_music: Path | None,
    output_path: Path,
    duration: float,
    base_volume: float = 100.0,
    music_volume: float = 15.0,
    threads: int | None = None,
) -> list[str]:
    """Loop the decorated short video with -c:v copy to full duration.

    Voice audio comes from the long V1 base video (identical to the
    legacy path). Music is looped and mixed exactly like the legacy
    path (same gains, same amix+alimiter parameters).
    """
    from services.audio_mix_service import _threads_args

    has_music = background_music is not None
    if has_music and not Path(background_music).exists():
        raise ValueError("background_music file missing for loop-copy mix")

    base_gain = base_volume / 100.0
    music_gain = music_volume / 100.0

    command = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", str(loop_video),
        "-i", str(base_video),
    ]
    if has_music:
        command += ["-stream_loop", "-1", "-i", str(background_music)]

    if has_music:
        audio_filter = (
            f"[1:a:0]volume={base_gain:.4f}[voice];"
            f"[2:a:0]volume={music_gain:.4f}[music];"
            f"[voice][music]"
            f"amix=inputs=2:duration=first:"
            f"dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95[mixed]"
        )
        audio_map = "[mixed]"
        needs_audio_encode = True
    elif abs(base_volume - 100.0) > 1e-9:
        audio_filter = f"[1:a:0]volume={base_gain:.4f}[aout]"
        audio_map = "[aout]"
        needs_audio_encode = True
    else:
        audio_filter = ""
        audio_map = "1:a:0?"
        needs_audio_encode = False

    if audio_filter:
        command += [
            "-filter_complex", audio_filter,
            "-map", "0:v:0",
            "-map", audio_map,
        ]
    else:
        command += ["-map", "0:v:0", "-map", audio_map]

    command += [
        "-t", f"{duration:.6f}",
        "-c:v", "copy",
    ]
    if needs_audio_encode:
        command += [*AUDIO_CODEC_ARGS]
    else:
        command += ["-c:a", "copy"]
    command += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero"]
    command += _threads_args(threads)
    command += ["-progress", "pipe:1", "-nostats", str(output_path)]
    return command
