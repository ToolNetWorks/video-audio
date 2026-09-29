"""Audio mix service: background-music modes for final render.

Three modes:
  - "default": system asset assets/audio/default_bgm.mp3
  - "upload":  user file stored under the job dir (cleaned with the job)
  - "none":    no background music, keep original video audio

The system asset is NEVER copied into jobs and NEVER cleaned up.
User uploads live under /var/lib/loop-video-audio/jobs/<job_id>/
so they disappear with normal job cleanup.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.template_service import TemplateOverlay

MUSIC_MODES = frozenset({"default", "upload", "none"})

# /root/loop-video-audio/assets/audio/default_bgm.mp3 (system asset)
DEFAULT_BGM_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "audio" / "default_bgm.mp3"
)

# Job root (must stay in sync with app.DATA_DIR)
JOBS_DIR = Path("/var/lib/loop-video-audio/jobs")


# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------

def estimated_music_loops(video_duration: float, music_duration: float) -> int:
    """Number of music loops needed to cover the video.

    math.ceil handles the exact-division case correctly:
    80min / 5min -> 16 (not 17).
    """
    if video_duration <= 0 or music_duration <= 0:
        return 0
    return max(1, math.ceil(video_duration / music_duration))


def resolve_music_path(
    *,
    music_mode: str,
    job_dir: Path,
    upload_path: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve the effective music file for a mode.

    Returns (music_path, music_source) where music_source is
    "system" / "upload" / None.

    Guard clauses: invalid mode and missing asset fail loudly,
    never with a silent fallback.
    """
    if music_mode not in MUSIC_MODES:
        raise ValueError(f"music_mode không hợp lệ: {music_mode}")

    if music_mode == "none":
        return None, None

    if music_mode == "default":
        if not DEFAULT_BGM_PATH.exists():
            raise FileNotFoundError("DEFAULT_MUSIC_NOT_FOUND")
        return DEFAULT_BGM_PATH, "system"

    # music_mode == "upload"
    if upload_path is None or not upload_path.exists():
        raise FileNotFoundError("BACKGROUND_MUSIC_NOT_FOUND")
    return upload_path, "upload"


def get_subtitle_burn_path(job_dir: Path) -> Path | None:
    """Return the subtitle file to burn, or None if disabled/missing.

    Prefers styled render.ass, falls back to working.srt.
    Never raises: no subtitle simply means no burn.
    """
    sub_dir = job_dir / "subtitle"
    meta_path = sub_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not meta.get("subtitle_enabled"):
        return None

    ass_path = sub_dir / "render.ass"
    if ass_path.exists():
        return ass_path

    srt_path = sub_dir / "working.srt"
    if srt_path.exists():
        return srt_path

    return None


def escape_subtitle_filter_path(path: Path) -> str:
    """Escape a path for ffmpeg subtitles filter (handles : and ')."""
    text = str(path).replace("\\", "/")
    text = text.replace(":", "\\:")
    text = text.replace("'", "'\\''")
    return f"'{text}'"


def subtitle_filter_arg(subtitle_path: Path) -> str:
    """Full `subtitles=` filter value with explicit fontsdir.

    libass is pointed at the bundled fonts dir
    (assets/fonts/Montserrat-SemiBold.ttf) so burns never depend
    on whatever the VPS happens to have installed via fontconfig.
    """
    from services.subtitle_style_service import FONTS_DIR

    return (
        f"subtitles={escape_subtitle_filter_path(subtitle_path)}"
        f":fontsdir={escape_subtitle_filter_path(FONTS_DIR)}"
    )


def music_filter_chain(
    base_volume: float,
    music_volume: float,
    *,
    music_offset: float = 0.0,
    music_duration: float | None = None,
) -> str:
    """Shared audio filter: voice + (looped) background music.

    music_offset > 0 starts the looped music at that position
    (preview seeks into the loop phase). It is implemented with
    atrim/aloop/concat inside the filter graph because input
    `-ss` combined with `-stream_loop` produces corrupt audio
    timestamps. music_offset <= 0 keeps the plain from-zero path.
    """
    base_gain = base_volume / 100.0
    music_gain = music_volume / 100.0

    if music_offset and music_offset > 0:
        if not music_duration or music_duration <= 0:
            raise ValueError(
                "music_duration is required for music offset"
            )
        music_src = (
            f"[1:a:0]"
            f"atrim=start={music_offset:.3f}:end={music_duration:.3f},"
            f"asetpts=PTS-STARTPTS[m1];"
            f"[1:a:0]aloop=loop=-1:size=2000000000[m2];"
            f"[m1][m2]concat=n=2:v=0:a=1[moff];"
            f"[moff]volume={music_gain:.4f}[music]"
        )
    else:
        music_src = (
            f"[1:a:0]volume={music_gain:.4f}[music]"
        )

    return (
        f"[0:a:0]volume={base_gain:.4f}[voice];"
        f"{music_src};"
        f"[voice][music]"
        f"amix=inputs=2:duration=first:"
        f"dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95[mixed]"
    )


def _seek_args(seconds: float | None) -> list[str]:
    """Input `-ss` args. Empty when None/<=0 (final path unchanged)."""
    if seconds is None or seconds <= 0:
        return []
    return ["-ss", f"{seconds:.3f}"]


def _threads_args(threads: int | None) -> list[str]:
    if threads is None:
        return []
    return ["-threads", str(int(threads))]


def _video_codec_args(encode_video: bool) -> list[str]:
    if encode_video:
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
    return ["-c:v", "copy"]


# ----------------------------------------------------------
# Command builders
# ----------------------------------------------------------

def build_mix_with_music_command(
    *,
    base_video: Path,
    background_music: Path,
    output_path: Path,
    duration: float,
    base_volume: float,
    music_volume: float,
    encode_video: bool,
    subtitle_path: Path | None = None,
    start_seconds: float | None = None,
    music_start_offset: float | None = None,
    music_duration: float | None = None,
    threads: int | None = None,
    template: TemplateOverlay | None = None,
) -> list[str]:
    """Final render WITH background music (looped via -stream_loop).

    start_seconds seeks base video/audio (preview). The music loop
    phase shifts inside the filter graph via music_start_offset
    (preview_start % music_duration). New params default to the
    classic full-render behavior.
    """
    if background_music is None:
        raise ValueError("background_music is required for music mix")

    from services.template_service import build_template_chain

    command = [
        "ffmpeg",
        "-y",
    ]

    command += _seek_args(start_seconds)
    command += ["-i", str(base_video)]

    # Loop nhạc nền vô hạn, cắt đúng độ dài bằng -t.
    # Không concat vật lý, không tạo MP3 trung gian.
    command += [
        "-stream_loop", "-1",
        "-i", str(background_music),
    ]

    audio_chain = music_filter_chain(
        base_volume,
        music_volume,
        music_offset=music_start_offset or 0.0,
        music_duration=music_duration,
    )

    if template is None:
        command += [
            "-filter_complex",
            audio_chain,
            "-map", "0:v:0",
            "-map", "[mixed]",
            "-t", f"{duration:.6f}",
        ]

        if subtitle_path is not None:
            command += [
                "-vf",
                subtitle_filter_arg(subtitle_path),
            ]
            # Burn subtitle bắt buộc encode lại video.
            encode_video = True
    else:
        # Template input is always index 2 here (0=video, 1=music).
        command += [
            "-loop", "1",
            "-i", str(template.path),
        ]

        tchain, _ = build_template_chain(
            input_label="2",
            overlay=template,
            src_label="[0:v]",
        )

        if subtitle_path is not None:
            video_chain = (
                tchain + "[t];[t]"
                + subtitle_filter_arg(subtitle_path)
                + "[vout]"
            )
            video_out = "[vout]"
            # Burn subtitle bắt buộc encode lại video.
            encode_video = True
        else:
            video_chain = tchain + "[tout]"
            video_out = "[tout]"

        command += [
            "-filter_complex",
            audio_chain + ";" + video_chain,
            "-map", video_out,
            "-map", "[mixed]",
            "-t", f"{duration:.6f}",
        ]

    command += _video_codec_args(encode_video)

    command += [
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
    ]

    command += _threads_args(threads)

    command += [
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]
    return command


def build_mix_without_music_command(
    *,
    base_video: Path,
    output_path: Path,
    duration: float,
    base_volume: float = 100.0,
    subtitle_path: Path | None = None,
    force_encode_video: bool = False,
    start_seconds: float | None = None,
    threads: int | None = None,
    template: TemplateOverlay | None = None,
) -> list[str]:
    """Final render WITHOUT background music.

    No amix, no extra music input. Keeps original video audio.
    Stream-copies whenever the pipeline allows it.
    """
    from services.template_service import build_template_chain

    needs_volume = abs(base_volume - 100.0) > 1e-9
    needs_encode = (
        force_encode_video
        or subtitle_path is not None
        or template is not None
    )

    command = [
        "ffmpeg",
        "-y",
    ]

    command += _seek_args(start_seconds)

    if template is None:
        command += [
            "-i", str(base_video),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-t", f"{duration:.6f}",
        ]

        if subtitle_path is not None:
            command += [
                "-vf",
                subtitle_filter_arg(subtitle_path),
            ]

        command += _video_codec_args(needs_encode)

        if needs_volume:
            command += [
                "-af", f"volume={base_volume / 100.0:.4f}",
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

    # Template path: video (and volume-scaled audio, if any) go
    # through filter_complex; template input is always index 1.
    command += [
        "-i", str(base_video),
        "-loop", "1",
        "-i", str(template.path),
    ]

    tchain, _ = build_template_chain(
        input_label="1",
        overlay=template,
        src_label="[0:v]",
    )

    if subtitle_path is not None:
        video_chain = (
            tchain + "[t];[t]"
            + subtitle_filter_arg(subtitle_path)
            + "[vout]"
        )
        video_out = "[vout]"
    else:
        video_chain = tchain + "[tout]"
        video_out = "[tout]"

    filter_complex = video_chain
    audio_map = "0:a:0?"

    if needs_volume:
        filter_complex += (
            f";[0:a:0]volume={base_volume / 100.0:.4f}[aout]"
        )
        audio_map = "[aout]"

    command += [
        "-filter_complex", filter_complex,
        "-map", video_out,
        "-map", audio_map,
        "-t", f"{duration:.6f}",
    ]

    command += _video_codec_args(True)

    if needs_volume:
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


def build_mix_command(
    *,
    base_video: Path,
    background_music: Path | None = None,
    output_path: Path,
    duration: float,
    base_volume: float = 100.0,
    music_volume: float = 15.0,
    encode_video: bool = False,
    subtitle_path: Path | None = None,
    force_encode_video: bool = False,
    start_seconds: float | None = None,
    music_start_offset: float | None = None,
    music_duration: float | None = None,
    threads: int | None = None,
    template: TemplateOverlay | None = None,
) -> list[str]:
    """Dispatcher: with music when a file is given, without otherwise.

    Guard clauses / early return — never builds "-i None",
    never probes a None path.
    """
    if background_music is None:
        return build_mix_without_music_command(
            base_video=base_video,
            output_path=output_path,
            duration=duration,
            base_volume=base_volume,
            subtitle_path=subtitle_path,
            force_encode_video=force_encode_video,
            start_seconds=start_seconds,
            threads=threads,
            template=template,
        )

    return build_mix_with_music_command(
        base_video=base_video,
        background_music=background_music,
        output_path=output_path,
        duration=duration,
        base_volume=base_volume,
        music_volume=music_volume,
        encode_video=encode_video,
        subtitle_path=subtitle_path,
        start_seconds=start_seconds,
        music_start_offset=music_start_offset,
        music_duration=music_duration,
        threads=threads,
        template=template,
    )
