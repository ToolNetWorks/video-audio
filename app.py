from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from services.audio_mix_service import (
    DEFAULT_BGM_PATH,
    MUSIC_MODES,
    build_mix_command,
    build_mix_with_music_command,
    build_mix_without_music_command,
    estimated_music_loops,
    get_subtitle_burn_path,
    resolve_music_path,
)

from services.logo_service import (
    LOGO_EXTENSIONS,
    RenderLogo,
    build_final_with_logo_command,
    clamp_config,
    find_logo_file,
    get_logo_dir,
    get_render_logo,
    load_logo_config,
    probe_video_dims,
    save_logo_config,
    validate_logo_image,
)

from services.final_preview_service import (
    PREVIEW_THREADS,
    build_preview_ass,
    cancel_preview,
    compute_config_hash,
    get_preview_status,
    is_preview_running,
    music_loop_offset,
    preview_paths,
    read_preview_state,
    validate_preview_params,
    write_preview_state,
)

from services.template_service import (
    TEMPLATE_EXTENSIONS,
    TEMPLATE_MODES,
    TemplateOverlay,
    delete_upload as service_delete_template_upload,
    get_system_template_path,
    get_template_dir,
    list_system_templates,
    load_template_config,
    reset_template_config,
    resolve_template,
    save_template_config,
    store_upload as store_template_upload,
    validate_template_upload,
)

from services.fast_final_service import (
    DECORATED_LOOP_COPY,
    FAST_COPY,
    FULL_ENCODE,
    build_decorated_loop_command,
    build_loop_copy_final_command,
    cache_paths,
    decide_final_render_mode,
    decorated_loop_cache_key,
    get_short_source,
    read_decorated_cache,
    write_decorated_cache,
)

from services.job_lifecycle_service import (
    MAX_AUDIO_BYTES,
    MAX_CONCURRENT_FFMPEG,
    MAX_IMAGE_BYTES,
    MAX_MUSIC_BYTES,
    MAX_SRT_BYTES,
    MAX_VIDEO_BYTES,
    SWEEP_INTERVAL_MIN,
    delete_job as delete_job_dir,
    job_dir as _lifecycle_job_dir,
    kill_job_processes,
    reconcile_startup,
    require_free_space,
    sweep_once,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        result = reconcile_startup()
        print(
            f"[lifecycle] startup reconcile: {result}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[lifecycle] reconcile failed: {exc}",
            flush=True,
        )

    stop = threading.Event()

    def _sweep_loop() -> None:
        while not stop.wait(SWEEP_INTERVAL_MIN * 60):
            try:
                print(
                    f"[lifecycle] sweep: {sweep_once()}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[lifecycle] sweep failed: {exc}",
                    flush=True,
                )

    threading.Thread(
        target=_sweep_loop,
        daemon=True,
    ).start()

    yield
    stop.set()


def require_job_dir(job_id: str) -> Path:
    """Validated job dir (404 on traversal/invalid ids)."""
    try:
        return _lifecycle_job_dir(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# Cap concurrent FFmpeg renders so one burst can't hang the VPS.
FFMPEG_SEMAPHORE = threading.BoundedSemaphore(
    max(1, MAX_CONCURRENT_FFMPEG)
)

from services.subtitle_service import (
    get_subtitle_content,
    get_subtitle_status,
    reset_subtitle_state,
    start_subtitle_job,
)
from services.subtitle_runtime_service import get_subtitle_runtime_status


DATA_DIR = Path("/var/lib/loop-video-audio/jobs")

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4v"
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac",
    ".flac", ".ogg", ".opus"
}

app = FastAPI(
    title="Loop Video + Audio",
    version="2.0",
    lifespan=lifespan,
)


HTML = r"""
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta
    name="viewport"
    content="width=device-width,initial-scale=1,viewport-fit=cover"
>
<title>Loop Video + Audio</title>

<style>
*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#0b0d10;
    color:#f5f5f5;
    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        sans-serif;
}

.wrap{
    max-width:800px;
    margin:40px auto;
    padding:18px;
}

.card{
    background:#15181d;
    border:1px solid #292d34;
    border-radius:20px;
    padding:26px;
}

h1{
    margin:0 0 8px;
    font-size:28px;
}

h2{
    margin:0 0 8px;
    font-size:22px;
}

.sub{
    color:#9299a3;
    line-height:1.5;
    margin-bottom:28px;
}

.field{
    margin:22px 0;
}

label{
    display:block;
    margin-bottom:9px;
    font-weight:700;
}

input[type=file]{
    width:100%;
    padding:15px;
    border:1px dashed #454b55;
    border-radius:12px;
    background:#0f1115;
    color:#ddd;
}

button,
.action{
    width:100%;
    border:0;
    border-radius:12px;
    padding:15px 18px;
    font-size:16px;
    font-weight:750;
    cursor:pointer;
    text-decoration:none;
    text-align:center;
    display:block;
}

.primary{
    background:#fff;
    color:#111;
}

.secondary{
    background:#0f1115;
    color:#fff;
    border:1px solid #454b55;
}

button:disabled{
    opacity:.45;
    cursor:not-allowed;
}

.info{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
    margin-top:22px;
}

.box{
    background:#0f1115;
    border:1px solid #292d34;
    border-radius:12px;
    padding:14px;
}

.box small{
    color:#858d98;
}

.box strong{
    display:block;
    margin-top:5px;
}

.progress{
    height:9px;
    overflow:hidden;
    border-radius:50px;
    background:#272b31;
    margin-top:18px;
}

.bar{
    width:0%;
    height:100%;
    background:#fff;
    transition:width .35s ease;
}

.runtime{
    display:none;
    align-items:center;
    gap:12px;
    margin-top:20px;
    padding:14px;
    border:1px solid #292d34;
    background:#0f1115;
    border-radius:12px;
}

.spinner{
    width:22px;
    height:22px;
    flex:0 0 22px;
    border:3px solid #343941;
    border-top-color:#fff;
    border-radius:50%;
    animation:spin .8s linear infinite;
}

@keyframes spin{
    to{ transform:rotate(360deg); }
}

.runtime-main{
    font-weight:750;
}

.runtime-detail{
    color:#9299a3;
    font-size:13px;
    margin-top:4px;
    line-height:1.5;
}

.status{
    color:#b7bec8;
    margin-top:14px;
    min-height:24px;
}

.error{
    color:#ff8585 !important;
}

#stage2{
    display:none;
}

video{
    width:100%;
    max-height:460px;
    background:#000;
    border-radius:14px;
    margin-top:18px;
}

.video-meta{
    color:#9ba3ae;
    margin:12px 0 20px;
    font-size:14px;
}

.volume-card{
    background:#0f1115;
    border:1px solid #292d34;
    padding:16px;
    border-radius:14px;
    margin:14px 0;
}

.volume-head{
    display:flex;
    justify-content:space-between;
    gap:12px;
    margin-bottom:10px;
}

.volume-head strong{
    font-size:14px;
}

.volume-value{
    color:#aab2bd;
}

input[type=range]{
    width:100%;
}

.hint{
    font-size:13px;
    color:#868f9a;
    line-height:1.5;
    margin-top:8px;
}

.radio-row{
    display:block;
    font-weight:700;
    margin-bottom:8px;
    cursor:pointer;
}

.radio-row input{
    margin-right:8px;
}

.preset-grid{
    display:grid;
    grid-template-columns:1fr 1fr 1fr;
    gap:8px;
    margin-top:8px;
}

.preset-grid button{
    min-height:44px;
    width:100%;
    border:1px solid #454b55;
    border-radius:10px;
    background:#0f1115;
    color:#fff;
    font-size:13px;
    font-weight:700;
    cursor:pointer;
    padding:8px 4px;
}

.logo-row{
    display:flex;
    align-items:center;
    gap:10px;
    margin:10px 0;
}

.logo-row input[type=checkbox]{
    width:22px;
    height:22px;
}

#videoPreviewWrapper{
    position:relative;
    width:100%;
    aspect-ratio:16/9;
    overflow:hidden;
    background:#000;
}

#baseVideo{
    display:block;
    width:100%;
    height:100%;
    object-fit:contain;
}



#templateOverlay{
    position:absolute;
    left:0;
    top:0;
    width:100%;
    height:100%;
    z-index:8;
    pointer-events:none;
    user-select:none;
    -webkit-user-select:none;
}

#logoOverlay{
    position:absolute;
    touch-action:none;
    z-index:9;
    user-select:none;
    -webkit-user-select:none;
    -webkit-user-drag:none;
    -webkit-touch-callout:none;
    pointer-events:auto;
}

.logo-editing #baseVideo{
    pointer-events:none !important;
}
.logo-editing #logoOverlay{
    pointer-events:auto !important;
    cursor:grab;
    z-index:50 !important;
}
.logo-editing #logoOverlay:active{
    cursor:grabbing;
}

#logoEditBar{
    display:none;
    background:#1a1a2e;
    border:1px solid #4ade80;
    border-radius:8px;
    padding:10px 14px;
    margin:10px 0;
    text-align:center;
    font-size:14px;
    color:#e0e0e0;
}
#logoEditBar.active{
    display:block;
}
#logoEditBar button{
    margin-top:8px;
    min-height:40px;
    padding:6px 24px;
    background:#4ade80;
    color:#111;
    border:none;
    border-radius:6px;
    font-weight:700;
    cursor:pointer;
    font-size:14px;
}
#logoEditBtn{
    min-height:44px;
    width:100%;
    margin-top:10px;
    background:#2563eb;
    color:#fff;
    border:none;
    border-radius:6px;
    font-weight:700;
    font-size:14px;
    cursor:pointer;
    padding:10px;
}
#logoEditBtn:disabled{
    opacity:0.4;
    cursor:not-allowed;
}

@media(max-width:600px){
    .preset-grid{
        grid-template-columns:1fr 1fr;
    }
}

.actions{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
    margin-top:18px;
}

.final-box{
    display:none;
    margin-top:28px;
    padding-top:24px;
    border-top:1px solid #292d34;
}

.badge{
    display:inline-block;
    padding:6px 9px;
    font-size:12px;
    border-radius:999px;
    background:#242932;
    color:#bec6d1;
    margin-bottom:10px;
}

@media(max-width:600px){
    .wrap{
        margin:8px auto;
        padding:12px;
    }

    .card{
        padding:20px;
    }

    .info,
    .actions{
        grid-template-columns:1fr;
    }
}
</style>
</head>

<body>
<div class="wrap">

<div class="card">

    <!-- ========================= -->
    <!-- STAGE 1 -->
    <!-- ========================= -->

    <section id="stage1">

        <h1>Loop Video + Audio</h1>

        <div class="sub">
            Lặp video ngắn đến khi audio kết thúc và xuất thành
            một video hoàn chỉnh.
        </div>

        <div class="field">
            <label>1. Video tĩnh / video loop</label>
            <input
                id="videoInput"
                type="file"
                accept="video/*"
            >
        </div>

        <div class="field">
            <label>2. Audio MP3 / M4A / WAV</label>

            <!-- Không dùng accept để Safari iPhone cho chọn MP3 -->
            <input
                id="audioInput"
                type="file"
            >
        </div>

        <button
            id="createButton"
            class="primary"
        >
            CREATE VIDEO
        </button>

        <div class="info">

            <div class="box">
                <small>Video</small>
                <strong id="videoInfo">
                    Chưa chọn
                </strong>
            </div>

            <div class="box">
                <small>Audio</small>
                <strong id="audioInfo">
                    Chưa chọn
                </strong>
            </div>

        </div>

        <div
            id="renderRuntime"
            class="runtime"
        >
            <div
                id="renderSpinner"
                class="spinner"
            ></div>

            <div>
                <div
                    id="renderRuntimeMain"
                    class="runtime-main"
                >
                    Đang chuẩn bị...
                </div>

                <div
                    id="renderRuntimeDetail"
                    class="runtime-detail"
                >
                    Runtime 00:00:00
                </div>
            </div>
        </div>

        <div class="progress">
            <div
                id="renderBar"
                class="bar"
            ></div>
        </div>

        <div
            id="renderStatus"
            class="status"
        >
            Sẵn sàng.
        </div>

    </section>


    <!-- ========================= -->
    <!-- STAGE 2 -->
    <!-- ========================= -->

    <section id="stage2">

        <span class="badge">
            BƯỚC 2
        </span>

        <h1>Hoàn thiện âm thanh</h1>

        <div class="sub">
            Xem video vừa tạo, thêm nhạc nền và cân chỉnh
            âm lượng trước khi render bản cuối.
        </div>

        <div id="stepIndicator" style="display:flex; gap:8px; margin:14px 0 18px;">
            <div class="step-dot active" data-step="2">2</div>
            <div class="step-line"></div>
            <div class="step-dot" data-step="3">3</div>
            <div class="step-line"></div>
            <div class="step-dot" data-step="4">4</div>
        </div>

        <style>
            .step-dot{width:32px;height:32px;border-radius:50%;background:#272b31;color:#858d98;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;}
            .step-dot.active{background:#fff;color:#111;}
            .step-dot.done{background:#4ade80;color:#111;}
            .step-line{flex:1;height:2px;background:#272b31;margin:0 4px;}
        </style>

        <div id="step2" class="step-panel">
        <div id="videoPreviewWrapper">
        <video
            id="baseVideo"
            controls
            playsinline
            preload="metadata"
        ></video>
        <div id="templateOverlay" style="display:none;"></div>
        <img id="logoOverlay" draggable="false" style="display:none;" alt="">
        <div id="customSubOverlay" style="display:none;"></div>
        </div>

        <div
            id="baseVideoMeta"
            class="video-meta"
        ></div>

        <div class="field">

            <label>
                Nhạc nền
            </label>

            <div id="musicModeOptions">
                <label class="radio-row">
                    <input
                        type="radio"
                        name="musicMode"
                        value="default"
                        checked
                    >
                    Nhạc mặc định
                </label>

                <label class="radio-row">
                    <input
                        type="radio"
                        name="musicMode"
                        value="upload"
                    >
                    Tải MP3 riêng
                </label>

                <label class="radio-row">
                    <input
                        type="radio"
                        name="musicMode"
                        value="none"
                    >
                    Không dùng nhạc
                </label>
            </div>

            <div
                id="defaultMusicPanel"
                class="box"
                style="margin-top:12px;"
            >
                <div>
                    Nhạc mặc định
                </div>

                <button
                    id="defaultPreviewButton"
                    class="secondary"
                    type="button"
                    style="margin-top:8px;"
                >
                    ▶ Preview
                </button>

                <div class="hint">
                    Dùng nhạc nền mặc định của hệ thống,
                    tự loop đến hết video.
                </div>
            </div>

            <div
                id="uploadMusicPanel"
                style="display:none; margin-top:12px;"
            >
                <input
                    id="backgroundMusic"
                    type="file"
                >

                <div
                    id="uploadMusicMeta"
                    class="hint"
                ></div>

                <button
                    id="uploadPreviewButton"
                    class="secondary"
                    type="button"
                    style="margin-top:8px; display:none;"
                >
                    ▶ Preview
                </button>
            </div>

            <div
                id="noneMusicPanel"
                class="box"
                style="display:none; margin-top:12px;"
            >
                Không sử dụng nhạc nền.
            </div>

            <div class="hint">
                Nếu video dài 50 phút nhưng nhạc nền chỉ dài
                5 phút, tool sẽ tự loop nhạc nền đến hết video.
            </div>

        </div>


        <div class="volume-card">

            <div class="volume-head">
                <strong>
                    Âm thanh video / giọng đọc
                </strong>

                <span
                    id="baseVolumeValue"
                    class="volume-value"
                >
                    100%
                </span>
            </div>

            <input
                id="baseVolume"
                type="range"
                min="0"
                max="100"
                value="100"
                step="1"
            >

        </div>


        <div
            id="musicVolumeCard"
            class="volume-card"
        >

            <div class="volume-head">
                <strong>
                    Nhạc nền
                </strong>

                <span
                    id="musicVolumeValue"
                    class="volume-value"
                >
                    15%
                </span>
            </div>

            <input
                id="musicVolume"
                type="range"
                min="0"
                max="100"
                value="15"
                step="1"
            >

            <div class="hint">
                Gợi ý cho audio truyện: nhạc nền khoảng
                8–20%.
            </div>

        </div>

        <!-- Audio này chỉ dùng để preview trong browser -->
        <audio
            id="backgroundPreview"
            loop
            preload="metadata"
        ></audio>

        <div class="hint">
            Khi đã chọn nhạc nền, bấm Play trên video để
            nghe thử video + nhạc nền cùng lúc.
        </div>


        <!-- ========================= -->
        <!-- SUBTITLE -->
        <!-- ========================= -->

        <div
            id="subtitleCard"
            class="volume-card"
            style="margin-top:18px;"
        >

            <div class="volume-head">
                <strong>
                    Phụ đề
                </strong>
            </div>

            <div id="subtitleOptions">
                <label style="display:block; margin-bottom:8px; font-weight:700;">
                    <input
                        type="radio"
                        name="subtitleMode"
                        value="none"
                        checked
                    >
                    Không dùng subtitle
                </label>

                <label style="display:block; margin-bottom:8px; font-weight:700;">
                    <input
                        type="radio"
                        name="subtitleMode"
                        value="colab"
                    >
                    Tự tạo SRT tiếng Việt bằng Google Colab
                </label>

                <label style="display:block; margin-bottom:8px; font-weight:700;">
                    <input
                        type="radio"
                        name="subtitleMode"
                        value="upload"
                    >
                    Upload SRT có sẵn
                </label>
            </div>


            <div id="subtitleUploadPanel" style="display:none; margin-top:12px;" class="box">
                <div class="hint">Upload file .srt có sẵn từ máy của bạn.</div>
                <input type="file" id="initialSrtInput" accept=".srt" style="display:block; margin-top:8px;">
                <button id="btnInitialUploadSrt" class="primary" style="margin-top:8px;">UPLOAD</button>
            </div>
            <div
                id="subtitleColabPanel"
                style="display:none; margin-top:12px;"
            >

                <div class="hint">
                    Tạo phụ đề tiếng Việt từ audio bằng Google Colab.
                </div>

                <div style="margin-top:12px;">
                    <label style="font-weight:700; margin-bottom:6px; display:block;">
                        Model:
                    </label>

                    <label style="display:block; margin-bottom:4px;">
                        <input type="radio" name="subtitleModel" value="auto" checked>
                        Auto (Tự chọn theo độ dài)
                    </label>
                    <label style="display:block; margin-bottom:4px;">
                        <input type="radio" name="subtitleModel" value="gipformer1.5-68M-rnnt">
                        Gipformer 1.5 68M RNNT
                    </label>
                    <label style="display:block; margin-bottom:4px;">
                        <input type="radio" name="subtitleModel" value="Zipformer-30M">
                        Zipformer-30M
                    </label>
                </div>
                <div class="box" style="margin-top:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <strong>Gipformer 1.5:</strong>
                        <span id="gipformerStatusBadge" class="badge bg-secondary">Checking...</span>
                    </div>
                    <small id="gipformerStatusText" style="display:block; margin-top:4px;">...</small>
                    <button id="btnInstallGipformer" class="primary" style="margin-top:8px; display:none; padding: 4px 8px; font-size: 12px;">
                        Tải model vào Drive
                    </button>
                </div>



                <div id="colabPanel" class="box" style="margin-top:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <strong>Colab</strong>
                        <span id="colabStatusBadge" class="badge bg-secondary">Checking...</span>
                    </div>
                    <small id="colabStatusText" style="display:block; margin-top:4px;">...</small>
                </div>

                <div id="driveAuthPanel" class="box" style="margin-top:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <strong>Google Drive</strong>
                        <span id="driveStatusBadge" class="badge bg-secondary">Checking...</span>
                    </div>
                    <small id="driveStatusText" style="display:block; margin-top:4px;">Session mới cần cấp lại quyền Drive.</small>
                    <small id="driveAuthError" style="display:none; margin-top:4px; color:#dc3545;"></small>
                    <button id="btnConnectDrive" class="primary" style="margin-top:8px; display:none; width: 100%; min-height: 44px;">
                        Kết nối Google Drive
                    </button>
                    <div id="driveAuthLinkContainer" style="display:none; margin-top:8px;">
                        <a id="driveAuthLink" href="#" target="_blank" rel="noopener noreferrer" class="button primary" style="display:block; text-align:center; text-decoration:none; width:100%; min-height: 44px; line-height: 44px; margin-bottom: 8px; box-sizing: border-box;">
                            Đăng nhập Google Drive
                        </a>
                        <button id="btnConfirmAuth" class="primary" style="width: 100%; min-height: 44px; background: #28a745; margin-bottom: 8px;">
                            Tôi đã cấp quyền
                        </button>
                        <button id="btnCancelAuth" class="button" style="width: 100%; min-height: 44px; background: #dc3545; color: white;">
                            Hủy
                        </button>
                    </div>
                </div>
                <button
                    id="subtitleButton"
                    class="primary"
                    style="margin-top:12px;"
                >
                    TẠO SRT TIẾNG VIỆT
                </button>

            </div>

            <div
                id="subtitleRuntime"
                class="runtime"
                style="display:none; margin-top:12px;"
            >

                <div
                    id="subtitleSpinner"
                    class="spinner"
                ></div>

                <div>
                    <div
                        id="subtitleRuntimeMain"
                        class="runtime-main"
                    >
                        Đang chuẩn bị...
                    </div>

                    <div
                        id="subtitleRuntimeDetail"
                        class="runtime-detail"
                    >
                        Runtime 00:00:00
                    </div>
                </div>

            </div>

            <div class="progress" style="margin-top:10px;">
                <div
                    id="subtitleBar"
                    class="bar"
                    style="width:0%"
                ></div>
            </div>

            <div
                id="subtitleStatus"
                class="status"
            >
                Chọn chế độ tạo subtitle.
            </div>

            <div
                id="subtitleResult"
                style="display:none; margin-top:14px;"
            >
                <div style="color:#4ade80; font-weight:700;">
                    ✓ Subtitle đã tạo
                </div>

                <div
                    id="subtitleResultMeta"
                    class="hint"
                    style="margin-top:4px;"
                ></div>

                <div class="actions" style="margin-top:10px;">

                    <button
                        id="viewSrtButton"
                        class="secondary"
                    >
                        XEM SRT
                    </button>

                    <a
                        id="downloadSrtButton"
                        class="action secondary"
                        download
                    >
                        DOWNLOAD SRT
                    </a>

                    <button
                        id="retrySubtitleButton"
                        class="primary"
                    >
                        TẠO LẠI
                    </button>

                </div>
            </div>

            <div
                id="subtitleView"
                style="display:none; margin-top:10px;"
            >
                <pre
                    id="srtContent"
                    style="
                        background:#0f1115;
                        border:1px solid #292d34;
                        border-radius:12px;
                        padding:14px;
                        max-height:300px;
                        overflow:auto;
                        font-size:13px;
                        color:#ddd;
                        white-space:pre-wrap;
                    "
                ></pre>

                <button
                    id="closeSrtButton"
                    class="secondary"
                    style="margin-top:8px;"
                >
                    ĐÓNG
                </button>
            </div>

        </div>


        <!-- ========================= -->
        <!-- LOGO / WATERMARK -->
        <!-- ========================= -->

        <div
            id="logoCard"
            class="volume-card"
            style="margin-top:18px;"
        >

            <div class="volume-head">
                <strong>
                    Logo / Watermark
                </strong>
            </div>

            <label class="logo-row">
                <input
                    id="logoEnabled"
                    type="checkbox"
                >
                <span>Bật logo</span>
            </label>

            <div class="field" style="margin:12px 0;">
                <input
                    id="logoInput"
                    type="file"
                    accept=".png,.webp,.jpg,.jpeg"
                >

                <div
                    id="logoMeta"
                    class="hint"
                ></div>
            </div>

            <div style="font-weight:700; font-size:14px;">
                Vị trí
            </div>

            <div class="preset-grid">
                <button type="button" data-preset="top-left">
                    Trên trái
                </button>

                <button type="button" data-preset="top-right">
                    Trên phải
                </button>

                <button type="button" data-preset="bottom-left">
                    Dưới trái
                </button>

                <button type="button" data-preset="bottom-right">
                    Dưới phải
                </button>

                <button type="button" data-preset="center">
                    Giữa
                </button>
            </div>

            <div class="volume-card" style="margin:14px 0 0;">

                <div class="volume-head">
                    <strong>
                        Kích thước logo
                    </strong>

                    <span
                        id="logoSizeValue"
                        class="volume-value"
                    >
                        12%
                    </span>
                </div>

                <input
                    id="logoSize"
                    type="range"
                    min="3"
                    max="40"
                    value="12"
                    step="1"
                >

            </div>

            <div class="volume-card" style="margin:14px 0 0;">

                <div class="volume-head">
                    <strong>
                        Độ trong suốt
                    </strong>

                    <span
                        id="logoOpacityValue"
                        class="volume-value"
                    >
                        85%
                    </span>
                </div>

                <input
                    id="logoOpacity"
                    type="range"
                    min="0"
                    max="100"
                    value="85"
                    step="1"
                >

            </div>

            <div class="actions" style="margin-top:14px;">
                <button
                    id="logoReset"
                    type="button"
                    class="secondary"
                    style="min-height:44px;"
                >
                    Reset logo
                </button>

                <button
                    id="logoDelete"
                    type="button"
                    class="secondary"
                    style="min-height:44px;"
                >
                    Xóa logo
                </button>
            </div>

            <button
                id="logoEditBtn"
                type="button"
                disabled
            >
                ✋ Chỉnh vị trí logo
            </button>

            <div id="logoEditBar">
                <div>📌 Đang chỉnh logo — kéo logo bằng tay để di chuyển.</div>
                <button type="button" id="logoEditDone">✓ Xong</button>
            </div>

            <div class="hint">
                Bấm "Chỉnh vị trí logo" rồi kéo trực tiếp trên video.
                Pinch 2 ngón (hoặc slider) để đổi kích thước.
            </div>

        </div>


        <!-- ========================= -->
        <!-- TEMPLATE / FRAME -->
        <!-- ========================= -->

        <div
            id="templateCard"
            class="volume-card"
            style="margin-top:18px;"
        >

            <div class="volume-head">
                <strong>
                    Template / Khung video
                </strong>
            </div>

            <div id="templateModeOptions">
                <label class="radio-row">
                    <input
                        type="radio"
                        name="templateMode"
                        value="system"
                        checked
                    >
                    Template mặc định
                </label>

                <label class="radio-row">
                    <input
                        type="radio"
                        name="templateMode"
                        value="upload"
                    >
                    Tải template riêng
                </label>

                <label class="radio-row">
                    <input
                        type="radio"
                        name="templateMode"
                        value="none"
                    >
                    Không dùng template
                </label>
            </div>

            <div
                id="templateSystemPanel"
                class="box"
                style="margin-top:12px;"
            >
                <div style="font-weight:700; font-size:14px;">
                    Template
                </div>

                <select
                    id="templateSelect"
                    style="width:100%; margin-top:8px; min-height:44px; padding:8px 12px; border-radius:10px; border:1px solid #454b55; background:#0f1115; color:#fff; font-size:15px;"
                >
                </select>

                <img
                    id="templateThumb"
                    alt="Template preview"
                    style="display:none; width:100%; max-width:320px; margin-top:10px; border-radius:10px; border:1px solid #292d34;"
                >
            </div>

            <div
                id="templateUploadPanel"
                style="display:none; margin-top:12px;"
            >
                <input
                    id="templateInput"
                    type="file"
                    accept=".svg,.png,.webp"
                >

                <div
                    id="templateMeta"
                    class="hint"
                ></div>

                <button
                    id="templateDeleteUpload"
                    type="button"
                    class="secondary"
                    style="min-height:44px; margin-top:8px; display:none;"
                >
                    Xóa template đã tải
                </button>
            </div>

            <div
                id="templateNonePanel"
                class="box"
                style="display:none; margin-top:12px;"
            >
                Không sử dụng template.
            </div>

            <div class="volume-card" style="margin:14px 0 0;">

                <div class="volume-head">
                    <strong>
                        Độ trong suốt template
                    </strong>

                    <span
                        id="templateOpacityValue"
                        class="volume-value"
                    >
                        100%
                    </span>
                </div>

                <input
                    id="templateOpacity"
                    type="range"
                    min="0"
                    max="100"
                    value="100"
                    step="1"
                >

            </div>

            <div class="actions" style="margin-top:14px;">
                <button
                    id="templateReset"
                    type="button"
                    class="secondary"
                    style="min-height:44px;"
                >
                    Reset template
                </button>
            </div>

            <div class="hint">
                Template phủ toàn bộ khung video.
                Logo nằm trên template, phụ đề luôn trên cùng.
            </div>

        </div>

        <div class="actions" style="margin-top:18px;">
            <button id="continueToPreview" type="button" class="primary" style="min-height:44px;">
                TIẾP TỤC → XEM TRƯỚC
            </button>
        </div>

        <div id="step2Validation" class="status" style="margin-top:8px;"></div>

    </div>

    <div id="step3" class="step-panel" style="display:none;">
        <span class="badge">BƯỚC 3</span>
        <h1>Xem thử video cuối</h1>
        <div class="sub">Render thử một đoạn ngắn với đầy đủ nhạc, phụ đề và logo trước khi render toàn bộ.</div>

        <div class="actions" style="margin-top:14px;">
            <button id="backToCustomize" type="button" class="secondary" style="min-height:44px;">
                ← QUAY LẠI CHỈNH SỬA
            </button>
        </div>


        <div
            id="previewCard"
            class="volume-card"
            style="margin-top:18px;"
        >

            <div class="volume-head">
                <strong>
                    Xem thử video cuối
                </strong>
            </div>

            <div class="hint">
                Render thử một đoạn ngắn với đầy đủ nhạc,
                phụ đề và logo trước khi render toàn bộ.
            </div>

            <div style="font-weight:700; font-size:14px; margin-top:12px;">
                Thời lượng preview
            </div>

            <div id="previewDurationOptions" style="margin-top:8px;">
                <label class="radio-row">
                    <input
                        type="radio"
                        name="previewDuration"
                        value="15"
                        checked
                    >
                    15 giây
                </label>

                <label class="radio-row">
                    <input
                        type="radio"
                        name="previewDuration"
                        value="30"
                    >
                    30 giây
                </label>
            </div>

            <div style="font-weight:700; font-size:14px; margin-top:12px;">
                Vị trí bắt đầu
            </div>

            <div style="display:flex; gap:8px; margin-top:8px;">
                <input
                    id="previewStartInput"
                    type="text"
                    inputmode="numeric"
                    placeholder="00:00:00 hoặc giây"
                    value="00:00:00"
                    style="flex:1; min-width:0; padding:12px; border-radius:10px; border:1px solid #454b55; background:#0f1115; color:#fff; font-size:16px;"
                >
            </div>

            <div class="preset-grid">
                <button
                    type="button"
                    id="previewStartHead"
                >
                    Đầu video
                </button>

                <button
                    type="button"
                    id="previewStartMid"
                >
                    Giữa video
                </button>

                <button
                    type="button"
                    id="previewStartTail"
                >
                    Cuối video
                </button>
            </div>

            <div class="actions" style="margin-top:14px;">
                <button
                    id="previewButton"
                    type="button"
                    class="primary"
                    style="min-height:44px;"
                >
                    XEM THỬ PREVIEW
                </button>

                <button
                    id="previewCancelButton"
                    type="button"
                    class="secondary"
                    style="min-height:44px; display:none;"
                >
                    HỦY PREVIEW
                </button>
            </div>

            <div
                id="previewStale"
                class="hint"
                style="display:none; color:#ffd97a;"
            >
                Thiết lập đã thay đổi. Hãy render preview lại.
            </div>

            <div class="progress" style="margin-top:10px;">
                <div
                    id="previewBar"
                    class="bar"
                    style="width:0%"
                ></div>
            </div>

            <div
                id="previewStatus"
                class="status"
            >
                Chưa có preview.
            </div>

            <div
                id="previewPlayerBox"
                style="display:none; margin-top:12px;"
            >
                <div style="color:#4ade80; font-weight:700;">
                    ✓ Preview sẵn sàng
                </div>

                <video
                    id="previewVideo"
                    controls
                    playsinline
                    preload="metadata"
                    style="width:100%; margin-top:10px;"
                ></video>
            </div>

            <div id="previewContinueBox"
                 class="actions"
                 style="display:none; margin-top:16px;">

                <button
                    id="continueToFinal"
                    type="button"
                    class="primary"
                    style="min-height:52px; width:100%;">
                    TIẾP TỤC → RENDER FINAL
                </button>

            </div>

        </div>

    </div>


    <div id="step4" class="step-panel" style="display:none;">
        <span class="badge">BƯỚC 4</span>
        <h1>Render video cuối</h1>
        <div class="sub">Tạo video hoàn chỉnh với tất cả thiết lập bạn đã chọn.</div>

        <div class="actions" style="margin-top:14px;">
            <button id="backToPreview" type="button" class="secondary" style="min-height:44px;">
                ← QUAY LẠI PREVIEW
            </button>
            <button id="mixButton" type="button" class="primary" style="min-height:44px;">
                RENDER TOÀN BỘ VIDEO
            </button>
        </div>

        <div
            id="mixRuntime"
            class="runtime"
        >

            <div
                id="mixSpinner"
                class="spinner"
            ></div>

            <div>
                <div
                    id="mixRuntimeMain"
                    class="runtime-main"
                >
                    Đang chuẩn bị...
                </div>

                <div
                    id="mixRuntimeDetail"
                    class="runtime-detail"
                >
                    Runtime 00:00:00
                </div>
            </div>

        </div>

        <div class="progress">
            <div
                id="mixBar"
                class="bar"
            ></div>
        </div>

        <div
            id="mixStatus"
            class="status"
        >
            Sẵn sàng render final.
        </div>


        <!-- ========================= -->
        <!-- FINAL RESULT -->
        <!-- ========================= -->

        <div
            id="finalBox"
            class="final-box"
        >

            <span class="badge">
                HOÀN TẤT
            </span>

            <h2>
                Video cuối
            </h2>

            <video
                id="finalVideo"
                controls
                playsinline
                preload="metadata"
            ></video>

            <a
                id="downloadFinal"
                class="action primary"
                style="margin-top:14px"
            >
                DOWNLOAD VIDEO FINAL
            </a>

        </div>

    </div>

    </section>

</div>
</div>


<script>
const videoInput =
    document.getElementById("videoInput");

const audioInput =
    document.getElementById("audioInput");

const createButton =
    document.getElementById("createButton");

const videoInfo =
    document.getElementById("videoInfo");

const audioInfo =
    document.getElementById("audioInfo");

const stage1 =
    document.getElementById("stage1");

const stage2 =
    document.getElementById("stage2");

const renderRuntime =
    document.getElementById("renderRuntime");

const renderSpinner =
    document.getElementById("renderSpinner");

const renderRuntimeMain =
    document.getElementById("renderRuntimeMain");

const renderRuntimeDetail =
    document.getElementById("renderRuntimeDetail");

const renderBar =
    document.getElementById("renderBar");

const renderStatus =
    document.getElementById("renderStatus");


const baseVideo =
    document.getElementById("baseVideo");

const baseVideoMeta =
    document.getElementById("baseVideoMeta");

const backgroundMusic =
    document.getElementById("backgroundMusic");

const backgroundPreview =
    document.getElementById("backgroundPreview");

const baseVolume =
    document.getElementById("baseVolume");

const musicVolume =
    document.getElementById("musicVolume");

const baseVolumeValue =
    document.getElementById("baseVolumeValue");

const musicVolumeValue =
    document.getElementById("musicVolumeValue");

const mixButton =
    document.getElementById("mixButton");

const downloadV1 =
    document.getElementById("downloadV1");

const mixRuntime =
    document.getElementById("mixRuntime");

const mixSpinner =
    document.getElementById("mixSpinner");

const mixRuntimeMain =
    document.getElementById("mixRuntimeMain");

const mixRuntimeDetail =
    document.getElementById("mixRuntimeDetail");

const mixBar =
    document.getElementById("mixBar");

const mixStatus =
    document.getElementById("mixStatus");

const finalBox =
    document.getElementById("finalBox");

const finalVideo =
    document.getElementById("finalVideo");

const downloadFinal =
    document.getElementById("downloadFinal");


let currentJobId = null;
let backgroundObjectUrl = null;


function formatSize(bytes) {
    if (bytes < 1024) {
        return bytes + " B";
    }

    if (bytes < 1024 * 1024) {
        return (bytes / 1024).toFixed(1) + " KB";
    }

    if (bytes < 1024 * 1024 * 1024) {
        return (
            bytes / 1024 / 1024
        ).toFixed(1) + " MB";
    }

    return (
        bytes / 1024 / 1024 / 1024
    ).toFixed(2) + " GB";
}


function sleep(ms) {
    return new Promise(resolve => {
        setTimeout(resolve, ms);
    });
}


function showError(element, text) {
    element.textContent = text;
    element.classList.add("error");
}


function clearError(element) {
    element.classList.remove("error");
}


videoInput.addEventListener(
    "change",
    () => {
        const file = videoInput.files[0];

        videoInfo.textContent =
            file
                ? formatSize(file.size)
                : "Chưa chọn";
    }
);


audioInput.addEventListener(
    "change",
    () => {
        const file = audioInput.files[0];

        audioInfo.textContent =
            file
                ? formatSize(file.size)
                : "Chưa chọn";
    }
);


baseVolume.addEventListener(
    "input",
    () => {
        const value = Number(baseVolume.value);

        baseVolumeValue.textContent =
            value + "%";

        baseVideo.volume =
            Math.min(1, value / 100);
    }
);


musicVolume.addEventListener(
    "input",
    () => {
        const value = Number(musicVolume.value);

        musicVolumeValue.textContent =
            value + "%";

        backgroundPreview.volume =
            Math.min(1, value / 100);
    }
);


function getMusicMode() {
    const checked =
        document.querySelector(
            'input[name="musicMode"]:checked'
        );

    return checked
        ? checked.value
        : "default";
}


function formatAudioTime(seconds) {
    if (
        !Number.isFinite(seconds) ||
        seconds < 0
    ) {
        return "";
    }

    const total =
        Math.floor(seconds);

    const minutes =
        Math.floor(total / 60);

    const secs =
        total % 60;

    return (
        String(minutes).padStart(2, "0")
        + ":"
        + String(secs).padStart(2, "0")
    );
}


function setBackgroundPreviewSrc(src) {
    if (backgroundObjectUrl) {
        URL.revokeObjectURL(
            backgroundObjectUrl
        );

        backgroundObjectUrl = null;
    }

    if (!src) {
        backgroundPreview.removeAttribute(
            "src"
        );

        backgroundPreview.load();

        return;
    }

    backgroundPreview.src = src;

    backgroundPreview.load();

    backgroundPreview.volume =
        Number(musicVolume.value) / 100;
}


function refreshMusicUI() {
    const mode = getMusicMode();

    const defaultPanel =
        document.getElementById(
            "defaultMusicPanel"
        );

    const uploadPanel =
        document.getElementById(
            "uploadMusicPanel"
        );

    const nonePanel =
        document.getElementById(
            "noneMusicPanel"
        );

    const volumeCard =
        document.getElementById(
            "musicVolumeCard"
        );

    if (defaultPanel) {
        defaultPanel.style.display =
            mode === "default"
                ? "block"
                : "none";
    }

    if (uploadPanel) {
        uploadPanel.style.display =
            mode === "upload"
                ? "block"
                : "none";
    }

    if (nonePanel) {
        nonePanel.style.display =
            mode === "none"
                ? "block"
                : "none";
    }

    if (volumeCard) {
        volumeCard.style.display =
            mode === "none"
                ? "none"
                : "block";
    }

    if (mode === "default") {
        setBackgroundPreviewSrc(
            "/api/assets/default-bgm"
        );

        mixButton.disabled = false;

        mixStatus.textContent =
            "Sẵn sàng render với nhạc mặc định.";

        return;
    }

    if (mode === "none") {
        setBackgroundPreviewSrc(null);

        mixButton.disabled = false;

        mixStatus.textContent =
            "Sẵn sàng render (không nhạc nền).";

        return;
    }

    // mode === "upload"
    const file =
        backgroundMusic.files[0];

    if (!file) {
        setBackgroundPreviewSrc(null);

        mixButton.disabled = true;

        mixStatus.textContent =
            "Chọn file nhạc nền để tiếp tục.";

        return;
    }

    mixButton.disabled = false;

    mixStatus.textContent =
        `Đã chọn nhạc nền: ${file.name}`;
}


document.querySelectorAll(
    'input[name="musicMode"]'
).forEach((radio) => {
    radio.addEventListener(
        "change",
        () => {
            clearError(mixStatus);

            refreshMusicUI();
            refreshPreviewStatus();
        }
    );
});


baseVolume.addEventListener(
    "change",
    () => {
        refreshPreviewStatus();
    }
);


musicVolume.addEventListener(
    "change",
    () => {
        refreshPreviewStatus();
    }
);


const defaultPreviewButton =
    document.getElementById(
        "defaultPreviewButton"
    );

if (defaultPreviewButton) {
    defaultPreviewButton.addEventListener(
        "click",
        async () => {
            if (
                getMusicMode() !== "default"
            ) {
                return;
            }

            try {
                if (
                    backgroundPreview.paused
                ) {
                    await backgroundPreview.play();
                } else {
                    backgroundPreview.pause();
                }
            } catch (_) {
                // iOS may require one direct interaction.
            }
        }
    );
}


const uploadPreviewButton =
    document.getElementById(
        "uploadPreviewButton"
    );

if (uploadPreviewButton) {
    uploadPreviewButton.addEventListener(
        "click",
        async () => {
            if (
                getMusicMode() !== "upload" ||
                !backgroundPreview.src
            ) {
                return;
            }

            try {
                if (
                    backgroundPreview.paused
                ) {
                    await backgroundPreview.play();
                } else {
                    backgroundPreview.pause();
                }
            } catch (_) {
                // ignore
            }
        }
    );
}


backgroundPreview.addEventListener(
    "loadedmetadata",
    () => {
        if (
            getMusicMode() !== "upload"
        ) {
            return;
        }

        const meta =
            document.getElementById(
                "uploadMusicMeta"
            );

        const file =
            backgroundMusic.files[0];

        if (meta && file) {
            meta.textContent =
                `✓ ${file.name} • `
                + formatAudioTime(
                    backgroundPreview.duration
                );
        }
    }
);


backgroundMusic.addEventListener(
    "change",
    () => {
        if (
            getMusicMode() !== "upload"
        ) {
            return;
        }

        const file =
            backgroundMusic.files[0];

        const meta =
            document.getElementById(
                "uploadMusicMeta"
            );

        const previewButton =
            document.getElementById(
                "uploadPreviewButton"
            );

        if (!file) {
            if (meta) {
                meta.textContent = "";
            }

            if (previewButton) {
                previewButton.style.display =
                    "none";
            }

            refreshMusicUI();

            return;
        }

        if (backgroundObjectUrl) {
            URL.revokeObjectURL(
                backgroundObjectUrl
            );
        }

        backgroundObjectUrl =
            URL.createObjectURL(file);

        backgroundPreview.src =
            backgroundObjectUrl;

        backgroundPreview.load();

        backgroundPreview.volume =
            Number(musicVolume.value) / 100;

        if (meta) {
            meta.textContent =
                `✓ ${file.name}`;
        }

        if (previewButton) {
            previewButton.style.display =
                "block";
        }

        mixButton.disabled = false;

        mixStatus.textContent =
            `Đã chọn nhạc nền: ${file.name}`;

        refreshPreviewStatus();
    }
);


// ======================================
// PREVIEW VIDEO + BGM
// ======================================

function syncBackgroundMusic() {
    if (!backgroundPreview.src) {
        return;
    }

    const duration =
        backgroundPreview.duration;

    if (
        !Number.isFinite(duration) ||
        duration <= 0
    ) {
        return;
    }

    const target =
        baseVideo.currentTime % duration;

    if (
        Math.abs(
            backgroundPreview.currentTime
            - target
        ) > 0.4
    ) {
        backgroundPreview.currentTime =
            target;
    }
}


baseVideo.addEventListener(
    "play",
    async () => {
        if (!backgroundPreview.src) {
            return;
        }

        syncBackgroundMusic();

        try {
            await backgroundPreview.play();
        } catch (_) {
            // iOS may require one direct interaction.
        }
    }
);


baseVideo.addEventListener(
    "pause",
    () => {
        backgroundPreview.pause();
    }
);


baseVideo.addEventListener(
    "ended",
    () => {
        backgroundPreview.pause();
    }
);


baseVideo.addEventListener(
    "seeked",
    () => {
        syncBackgroundMusic();
    }
);


baseVideo.addEventListener(
    "timeupdate",
    () => {
        if (
            baseVideo.paused ||
            !backgroundPreview.src
        ) {
            return;
        }

        syncBackgroundMusic();
    }
);


// ======================================
// STAGE 1
// ======================================

async function pollRender(jobId) {
    for (;;) {
        const response =
            await fetch(
                `/api/jobs/${jobId}`,
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Không đọc được trạng thái"
            );
        }

        const progress =
            Number(data.progress || 0);

        renderBar.style.width =
            `${Math.min(100, progress)}%`;

        renderRuntime.style.display =
            "flex";

        renderRuntimeMain.textContent =
            `Đang render ${progress.toFixed(1)}%`;

        let detail =
            `Runtime ${
                data.elapsed_text ||
                "00:00:00"
            }`;

        if (
            data.rendered_time_text &&
            data.audio_duration_text
        ) {
            detail +=
                ` • ${data.rendered_time_text}`
                + ` / ${data.audio_duration_text}`;
        }

        if (
            data.eta_text &&
            data.status !== "done"
        ) {
            detail +=
                ` • ETA ${data.eta_text}`;
        }

        renderRuntimeDetail.textContent =
            detail;

        renderStatus.textContent =
            data.message ||
            "Đang xử lý...";

        if (data.status === "done") {
            renderSpinner.style.display =
                "none";

            renderBar.style.width =
                "100%";

            renderRuntimeMain.textContent =
                "Hoàn tất 100%";

            showStage2(
                jobId,
                data
            );

            return;
        }

        if (data.status === "failed") {
            throw new Error(
                data.error ||
                "Render thất bại"
            );
        }

        await sleep(1000);
    }
}


function showStage2(
    jobId,
    data
) {
    currentJobId = jobId;

    stage1.style.display =
        "none";

    stage2.style.display =
        "block";

    baseVideo.src =
        `/api/jobs/${jobId}/preview?v=${Date.now()}`;

    baseVideo.volume =
        Number(baseVolume.value) / 100;

    baseVideoMeta.textContent =
        `Thời lượng: ${
            data.output_duration_text ||
            data.audio_duration_text ||
            ""
        }`;

    if (downloadV1) {
        downloadV1.href = `/api/jobs/${jobId}/download`;
    }

    refreshMusicUI();

    loadLogoState();

    loadSystemTemplates();

    loadTemplateState();

    refreshPreviewStatus();

    window.scrollTo({
        top: 0,
        behavior: "smooth"
    });
}


createButton.addEventListener(
    "click",
    async () => {

        clearError(renderStatus);

        const video =
            videoInput.files[0];

        const audio =
            audioInput.files[0];

        if (!video) {
            showError(
                renderStatus,
                "Chưa chọn video."
            );
            return;
        }

        if (!audio) {
            showError(
                renderStatus,
                "Chưa chọn audio."
            );
            return;
        }

        createButton.disabled = true;

        renderRuntime.style.display =
            "flex";

        renderSpinner.style.display =
            "block";

        renderRuntimeMain.textContent =
            "Đang upload...";

        renderRuntimeDetail.textContent =
            "Runtime 00:00:00";

        renderBar.style.width =
            "2%";

        renderStatus.textContent =
            "Đang upload video và audio...";

        const form =
            new FormData();

        form.append(
            "video",
            video
        );

        form.append(
            "audio",
            audio
        );

        try {
            const response =
                await fetch(
                    "/api/render",
                    {
                        method: "POST",
                        body: form
                    }
                );

            const data =
                await response.json();

            if (!response.ok) {
                throw new Error(
                    data.detail ||
                    "Không tạo được job"
                );
            }

            await pollRender(
                data.job_id
            );

        } catch (error) {

            renderBar.style.width =
                "0%";

            renderSpinner.style.display =
                "none";

            showError(
                renderStatus,
                error.message ||
                "Có lỗi xảy ra"
            );

            createButton.disabled =
                false;
        }
    }
);


// ======================================
// STEP WIZARD
// ======================================

let currentStep = 2;

function showStep(step) {
    currentStep = step;
    const panels = [document.getElementById("step2"), document.getElementById("step3"), document.getElementById("step4")];
    panels.forEach((panel, idx) => {
        if (!panel) return;
        panel.style.display = (idx + 2 === step) ? "block" : "none";
    });

    const dots = document.querySelectorAll(".step-dot");
    dots.forEach(dot => {
        const s = parseInt(dot.dataset.step, 10);
        dot.classList.toggle("active", s === step);
        dot.classList.toggle("done", s < step);
    });

    if (step === 3) {
        refreshPreviewStatus();
    }
    if (step === 2) {
        refreshSubtitleRuntimeStatus();
        requestAnimationFrame(() => {
            renderTemplateOverlay();
            renderLogoOverlay();
        });
    }
    window.scrollTo({top: 0, behavior: "smooth"});
}

document.getElementById("continueToPreview")?.addEventListener("click", async () => {
    const validation = validateStep2();
    if (!validation.ok) {
        const el = document.getElementById("step2Validation");
        if (el) {
            el.textContent = validation.message;
            el.classList.add("error");
        }
        return;
    }
    showStep(3);
});

document.getElementById("backToCustomize")?.addEventListener("click", () => showStep(2));
document.getElementById("backToPreview")?.addEventListener("click", () => showStep(3));

document
  .getElementById("continueToFinal")
  ?.addEventListener("click", () => {
      showStep(4);
  });

function validateStep2() {
    const musicMode = document.querySelector('input[name="musicMode"]:checked')?.value || "default";
    if (musicMode === "upload") {
        const file = document.getElementById("backgroundMusic")?.files[0];
        if (!file) {
            return {ok: false, message: "Chọn file nhạc nền để tiếp tục."};
        }
    }

    const subtitleMode = document.querySelector('input[name="subtitleMode"]:checked')?.value || "none";
    if (subtitleMode === "colab") {
        const srtReady = document.getElementById("subtitleResult")?.style.display === "block";
        if (!srtReady) {
            return {ok: false, message: "Bạn đã chọn phụ đề tự động nhưng SRT chưa sẵn sàng. Hãy tạo SRT trước."};
        }
    }

    const logoEnabled = document.getElementById("logoEnabled")?.checked;
    if (logoEnabled) {
        const logoFile = document.getElementById("logoInput")?.files[0];
        if (!logoFile) {
            return {ok: false, message: "Bật logo nhưng chưa chọn file logo."};
        }
    }

    const templateMode = document.querySelector('input[name="templateMode"]:checked')?.value || "system";
    if (templateMode === "upload") {
        const templateFile = document.getElementById("templateInput")?.files[0];
        if (!templateFile) {
            return {ok: false, message: "Chọn template riêng nhưng chưa chọn file."};
        }
    }

    return {ok: true};
}

async function refreshSubtitleRuntimeStatus() {
    try {
        const res = await fetch("/api/subtitle/runtime-status");
        if (!res.ok) return;
        const data = await res.json();

        const colabBadge = document.getElementById("colabStatusBadge");
        const colabText = document.getElementById("colabStatusText");
        const driveBadge = document.getElementById("driveStatusBadge");
        const driveText = document.getElementById("driveStatusText");
        const btnConnect = document.getElementById("btnConnectDrive");
        const linkContainer = document.getElementById("driveAuthLinkContainer");
        const authLink = document.getElementById("driveAuthLink");

        if (data.colab_connected) {
            if (colabBadge) {
                colabBadge.textContent = "Connected";
                colabBadge.className = "badge bg-success";
            }
            if (colabText) {
                colabText.textContent = data.colab_error || "Phiên Colab đã kết nối";
            }
        } else {
            if (colabBadge) {
                colabBadge.textContent = "Disconnected";
                colabBadge.className = "badge bg-danger";
            }
            if (colabText) {
                colabText.textContent = data.message || "Colab session chưa kết nối";
            }
        }

        if (data.drive_mounted) {
            if (driveBadge) {
                driveBadge.textContent = "Connected";
                driveBadge.className = "badge bg-success";
            }
            if (driveText) {
                driveText.textContent = data.drive_message || data.drive_error || "Google Drive đã kết nối";
            }
            if (btnConnect) btnConnect.style.display = "none";
            if (linkContainer) linkContainer.style.display = "none";
        } else if (data.auth_in_progress) {
            if (driveBadge) {
                driveBadge.textContent = "Waiting";
                driveBadge.className = "badge bg-warning text-dark";
            }
            if (driveText) {
                driveText.textContent = data.message || "Đang chờ...";
            }
            if (btnConnect) btnConnect.style.display = "none";
            if (data.oauth_url && authLink) {
                if (linkContainer) linkContainer.style.display = "block";
                authLink.href = data.oauth_url;
                syncConfirmAuthButton(data);
            }
        } else {
            if (driveBadge) {
                driveBadge.textContent = "Disconnected";
                driveBadge.className = "badge bg-danger";
            }
            if (driveText) {
                driveText.textContent = data.message || "Cần kết nối Drive";
            }
            if (btnConnect) btnConnect.style.display = "block";
            if (linkContainer) linkContainer.style.display = "none";
            if (data.drive_auth_error) showDriveAuthError(data.drive_auth_error);
        }
    } catch (e) {
        console.error(e);
    }
}

setInterval(refreshSubtitleRuntimeStatus, 5000);
refreshSubtitleRuntimeStatus();


// ======================================
// SUBTITLE
// ======================================

const subtitleCard =
    document.getElementById("subtitleCard");

const subtitleOptions =
    document.getElementById("subtitleOptions");

const subtitleColabPanel =
    document.getElementById("subtitleColabPanel");

const subtitleButton =
    document.getElementById("subtitleButton");

const subtitleRuntime =
    document.getElementById("subtitleRuntime");

const subtitleSpinner =
    document.getElementById("subtitleSpinner");

const subtitleRuntimeMain =
    document.getElementById("subtitleRuntimeMain");

const subtitleRuntimeDetail =
    document.getElementById("subtitleRuntimeDetail");

const subtitleBar =
    document.getElementById("subtitleBar");

const subtitleStatus =
    document.getElementById("subtitleStatus");

const subtitleResult =
    document.getElementById("subtitleResult");

const subtitleResultMeta =
    document.getElementById("subtitleResultMeta");

const viewSrtButton =
    document.getElementById("viewSrtButton");

const downloadSrtButton =
    document.getElementById("downloadSrtButton");

const retrySubtitleButton =
    document.getElementById("retrySubtitleButton");

const subtitleView =
    document.getElementById("subtitleView");

const srtContent =
    document.getElementById("srtContent");

const closeSrtButton =
    document.getElementById("closeSrtButton");

let subtitleJobId = null;
let subtitlePolling = false;


function showSubtitleError(element, text) {
    element.textContent = text;
    element.classList.add("error");
}


function clearSubtitleError(element) {
    element.classList.remove("error");
}


document.querySelectorAll(
    'input[name="subtitleMode"]'
).forEach((radio) => {
    radio.addEventListener(
        "change",
        () => {
            const mode =
                document
                    .querySelector(
                        'input[name="subtitleMode"]:checked'
                    )
                    .value;

            subtitleColabPanel.style.display =
                mode === "colab"
                    ? "block"
                    : "none";

            if (mode === "colab") {
                refreshSubtitleRuntimeStatus();
            }

            if (mode !== "colab") {
                subtitleResult.style.display = "none";
                subtitleView.style.display = "none";
            }

            clearSubtitleError(subtitleStatus);
        }
    );
});


subtitleButton.addEventListener(
    "click",
    async () => {
        clearSubtitleError(subtitleStatus);

        if (!currentJobId) {
            showSubtitleError(
                subtitleStatus,
                "Không tìm thấy job hiện tại."
            );
            return;
        }

        const mode =
            document
                .querySelector(
                    'input[name="subtitleMode"]:checked'
                )
                .value;

        if (mode !== "colab") {
            showSubtitleError(
                subtitleStatus,
                "Chọn chế độ Tự tạo SRT tiếng Việt bằng Google Colab."
            );
            return;
        }

        const model =
            document
                .querySelector(
                    'input[name="subtitleModel"]:checked'
                )
                .value;

        const runtime = await fetch("/api/subtitle/runtime-status").then(r => r.ok ? r.json() : null);
        if (!runtime) {
            showSubtitleError(subtitleStatus, "Không đọc được trạng thái Colab.");
            return;
        }
        if (!runtime.colab_connected) {
            showSubtitleError(subtitleStatus, "Phiên Colab chưa kết nối. Hãy kết nối Colab trước.");
            return;
        }
        if (!runtime.drive_mounted) {
            showSubtitleError(subtitleStatus, "Cần đăng nhập Google Drive trước.");
            return;
        }
        if (runtime.selected_model !== "auto" && !runtime.model_on_drive) {
            showSubtitleError(subtitleStatus, `Model ${runtime.selected_model} chưa có trên Drive.`);
            return;
        }

        subtitleButton.disabled = true;

        subtitleResult.style.display = "none";
        subtitleView.style.display = "none";

        subtitleRuntime.style.display = "flex";

        subtitleSpinner.style.display = "block";

        subtitleRuntimeMain.textContent =
            "Đang chuẩn bị...";

        subtitleRuntimeDetail.textContent =
            "Runtime 00:00:00";

        subtitleBar.style.width = "2%";

        subtitleStatus.textContent =
            "Đang tạo subtitle job...";

        const form = new FormData();

        form.append("model", model);

        try {
            const response =
                await fetch(
                    `/api/jobs/${currentJobId}/subtitle`,
                    {
                        method: "POST",
                        body: form
                    }
                );

            const data =
                await response.json();

            if (!response.ok) {
                const detail = data && data.detail;
                if (detail && detail.code === "DRIVE_AUTH_REQUIRED") {
                    throw new Error("Google Drive cần đăng nhập lại. Vui lòng bấm Kết nối Google Drive ở trên.");
                }
                if (detail && detail.code === "COLAB_SESSION_REQUIRED") {
                    throw new Error("Phiên Colab đã hết. Cần kết nối lại Colab.");
                }
                if (detail && detail.code === "MODEL_NOT_FOUND") {
                    throw new Error(`Model ${detail.model || ''} chưa có trên Drive.`);
                }
                throw new Error(
                    (detail && typeof detail === 'string' ? detail : false) ||
                    "Không tạo được subtitle job"
                );
            }

            subtitleJobId =
                data.subtitle_job_id ||
                currentJobId;

            await pollSubtitle(
                subtitleJobId
            );

        } catch (error) {

            subtitleSpinner.style.display =
                "none";

            showSubtitleError(
                subtitleStatus,
                error.message ||
                "Có lỗi xảy ra"
            );

            subtitleButton.disabled =
                false;
        }
    }
);


async function pollSubtitle(jobId) {
    subtitlePolling = true;

    for (;;) {
        const response =
            await fetch(
                `/api/jobs/${jobId}/subtitle-status`,
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Không đọc được trạng thái subtitle"
            );
        }

        const progress =
            Number(data.progress || 0);

        subtitleBar.style.width =
            `${Math.min(100, progress)}%`;

        subtitleRuntime.style.display =
            "flex";

        subtitleRuntimeMain.textContent =
            data.message ||
            "Đang xử lý...";

        let detail =
            `Runtime ${
                data.runtime_text ||
                "00:00:00"
            }`;

        if (data.audio_duration_text) {
            detail +=
                ` • Audio ${
                    data.audio_duration_text
                }`;

            if (data.processed_time_text) {
                detail +=
                    ` (${
                        data.processed_time_text
                    } đã xử lý)`;
            }
        }

        if (
            data.eta_text &&
            data.status !== "done" &&
            data.status !== "failed"
        ) {
            detail +=
                ` • ETA ${data.eta_text}`;
        }

        subtitleRuntimeDetail.textContent =
            detail;

        subtitleStatus.textContent =
            data.message ||
            "Đang xử lý...";

        if (data.status === "done") {

            subtitleSpinner.style.display =
                "none";

            subtitleBar.style.width =
                "100%";

            subtitleRuntimeMain.textContent =
                "Hoàn tất 100%";

            subtitleStatus.textContent =
                "Subtitle đã tạo";

            subtitleResult.style.display =
                "block";

            subtitleResultMeta.textContent =
                `Số đoạn: ${
                    data.segments || 0
                } • Duration: ${
                    data.audio_duration_text || ""
                }`;
                
            loadSubtitleEditor();

            downloadSrtButton.href =
                `/api/jobs/${jobId}/subtitle/download?v=${Date.now()}`;

            subtitleButton.disabled =
                false;

            subtitlePolling = false;

            return;
        }

        if (data.status === "failed") {

            subtitleSpinner.style.display =
                "none";

            let errorMsg = data.error || "Tạo SRT thất bại";
            if (errorMsg.includes("Colab session") || errorMsg.includes("session")) {
                errorMsg = "Phiên Colab đã hết. Cần kết nối lại.";
            } else if (errorMsg.includes("Drive") || errorMsg.includes("drive")) {
                errorMsg = "Google Drive cần đăng nhập lại.";
            } else if (errorMsg.includes("Model") || errorMsg.includes("model")) {
                errorMsg = "Model chưa sẵn sàng trên Drive.";
            }

            showSubtitleError(
                subtitleStatus,
                errorMsg
            );

            subtitleButton.disabled =
                false;

            subtitlePolling = false;

            return;
        }

        await sleep(1000);
    }
}


viewSrtButton.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        const response =
            await fetch(
                `/api/jobs/${currentJobId}/subtitle`,
                {
                    cache: "no-store"
                }
            );

        if (!response.ok) {
            showSubtitleError(
                subtitleStatus,
                "Không đọc được SRT"
            );
            return;
        }

        const text =
            await response.text();

        srtContent.textContent = text;

        subtitleView.style.display = "block";
    }
);


closeSrtButton.addEventListener(
    "click",
    () => {
        subtitleView.style.display = "none";
    }
);


retrySubtitleButton.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        subtitleResult.style.display = "none";
        subtitleView.style.display = "none";
        subtitleRuntime.style.display = "none";
        subtitleBar.style.width = "0%";
        subtitleStatus.textContent = "Đang thử lại...";

        try {
            await fetch(
                `/api/jobs/${currentJobId}/subtitle`,
                {
                    method: "DELETE"
                }
            );
        } catch (_) {
            // ignore
        }

        subtitleButton.click();
    }
);


// ======================================
// STAGE 2 MIX
// ======================================

async function pollMix(jobId) {
    for (;;) {

        const response =
            await fetch(
                `/api/jobs/${jobId}/mix-status`,
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Không đọc được trạng thái mix"
            );
        }

        const progress =
            Number(data.progress || 0);

        mixBar.style.width =
            `${Math.min(100, progress)}%`;

        mixRuntime.style.display =
            "flex";

        mixRuntimeMain.textContent =
            `Đang mix ${progress.toFixed(1)}%`;

        let detail =
            `Runtime ${
                data.elapsed_text ||
                "00:00:00"
            }`;

        if (
            data.rendered_time_text &&
            data.duration_text
        ) {
            detail +=
                ` • ${data.rendered_time_text}`
                + ` / ${data.duration_text}`;
        }

        if (
            data.eta_text &&
            data.status !== "done"
        ) {
            detail +=
                ` • ETA ${data.eta_text}`;
        }

        mixRuntimeDetail.textContent =
            detail;

        mixStatus.textContent =
            data.message ||
            "Đang mix nhạc nền...";

        if (data.status === "done") {

            mixSpinner.style.display =
                "none";

            mixBar.style.width =
                "100%";

            mixRuntimeMain.textContent =
                "Hoàn tất 100%";

            mixStatus.textContent =
                "Đã tạo video final.";

            showFinalVideo(
                jobId
            );

            mixButton.disabled =
                false;

            return;
        }

        if (data.status === "failed") {
            throw new Error(
                data.error ||
                "Mix thất bại"
            );
        }

        await sleep(1000);
    }
}


function showFinalVideo(jobId) {
    baseVideo.pause();
    backgroundPreview.pause();

    finalVideo.src =
        `/api/jobs/${jobId}/final-preview?v=${Date.now()}`;

    downloadFinal.href =
        `/api/jobs/${jobId}/final-download`;

    finalBox.style.display =
        "block";

    finalBox.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


mixButton.addEventListener(
    "click",
    async () => {

        clearError(mixStatus);

        if (!currentJobId) {
            showError(
                mixStatus,
                "Không tìm thấy job hiện tại."
            );
            return;
        }

        const musicMode =
            getMusicMode();

        const music =
            backgroundMusic.files[0];

        if (
            musicMode === "upload" &&
            !music
        ) {
            showError(
                mixStatus,
                "Chưa chọn nhạc nền."
            );
            return;
        }

        baseVideo.pause();
        backgroundPreview.pause();

        mixButton.disabled =
            true;

        finalBox.style.display =
            "none";

        mixRuntime.style.display =
            "flex";

        mixSpinner.style.display =
            "block";

        mixBar.style.width =
            "2%";

        mixRuntimeMain.textContent =
            musicMode === "upload"
                ? "Đang upload nhạc nền..."
                : "Đang chuẩn bị render final...";

        mixRuntimeDetail.textContent =
            "Runtime 00:00:00";

        mixStatus.textContent =
            "Đang chuẩn bị render final...";

        const form =
            new FormData();

        form.append(
            "music_mode",
            musicMode
        );

        if (
            musicMode === "upload" &&
            music
        ) {
            form.append(
                "background_music",
                music
            );
        }

        form.append(
            "base_volume",
            baseVolume.value
        );

        form.append(
            "music_volume",
            musicVolume.value
        );

        try {
            const response =
                await fetch(
                    `/api/jobs/${currentJobId}/mix`,
                    {
                        method: "POST",
                        body: form
                    }
                );

            const data =
                await response.json();

            if (!response.ok) {
                throw new Error(
                    data.detail ||
                    "Không tạo được mix job"
                );
            }

            await pollMix(
                currentJobId
            );

        } catch (error) {

            mixSpinner.style.display =
                "none";

            showError(
                mixStatus,
                error.message ||
                "Mix thất bại"
            );

            mixButton.disabled =
                false;
        }
    }
);

// ======================================
// TEMPLATE / FRAME
// ======================================

const templateSelect =
    document.getElementById("templateSelect");

const templateThumb =
    document.getElementById("templateThumb");

const templateInput =
    document.getElementById("templateInput");

const templateMeta =
    document.getElementById("templateMeta");

const templateOpacity =
    document.getElementById("templateOpacity");

const templateOpacityValue =
    document.getElementById("templateOpacityValue");

const templateDeleteUpload =
    document.getElementById("templateDeleteUpload");

let templateCfg = {
    mode: "system",
    enabled: true,
    system_template: "default",
    uploaded_file: null,
    opacity: 1.0,
};

let templateOverlayEl = null;
let systemTemplates = [];


function getTemplateMode() {
    const checked =
        document.querySelector(
            'input[name="templateMode"]:checked'
        );

    return checked
        ? checked.value
        : "system";
}


function renderTemplateOverlay() {
    templateOverlayEl = document.getElementById("templateOverlay");
    if (!templateOverlayEl) return;

    const mode = templateCfg.mode || "system";
    const show = templateCfg.enabled && mode !== "none" && (mode === "system" || !!templateCfg.uploaded_file);

    templateOverlayEl.style.display = show ? "block" : "none";
    if (!show) return;

    templateOverlayEl.style.opacity = String(templateCfg.opacity ?? 1);

    let src = "";
    if (mode === "system") {
        const tid = templateCfg.system_template || "default";
        src = `/api/templates/${tid}/asset?v=${Date.now()}`;
    } else {
        src = `/api/jobs/${currentJobId}/template/file?v=${Date.now()}`;
    }

    if (templateOverlayEl.dataset.src !== src) {
        templateOverlayEl.dataset.src = src;
        templateOverlayEl.style.backgroundImage = `url("${src}")`;
        templateOverlayEl.style.backgroundSize = "100% 100%";
        templateOverlayEl.style.backgroundRepeat = "no-repeat";
        templateOverlayEl.style.backgroundPosition = "center";
    }
}


function syncTemplateControls() {
    const mode = templateCfg.mode || "system";

    document.querySelectorAll(
        'input[name="templateMode"]'
    ).forEach((radio) => {
        radio.checked = radio.value === mode;
    });

    const systemPanel =
        document.getElementById("templateSystemPanel");

    const uploadPanel =
        document.getElementById("templateUploadPanel");

    const nonePanel =
        document.getElementById("templateNonePanel");

    if (systemPanel) {
        systemPanel.style.display =
            mode === "system" ? "block" : "none";
    }

    if (uploadPanel) {
        uploadPanel.style.display =
            mode === "upload" ? "block" : "none";
    }

    if (nonePanel) {
        nonePanel.style.display =
            mode === "none" ? "block" : "none";
    }

    if (
        templateSelect &&
        templateCfg.system_template
    ) {
        templateSelect.value =
            templateCfg.system_template;
    }

    if (templateThumb && mode === "system") {
        const tid =
            templateCfg.system_template || "default";

        templateThumb.src =
            `/api/templates/${tid}/preview`;

        templateThumb.style.display = "block";
    } else if (templateThumb) {
        templateThumb.style.display = "none";
    }

    const opacityPct = Math.round(
        (templateCfg.opacity ?? 1) * 100
    );

    templateOpacity.value = opacityPct;
    templateOpacityValue.textContent = opacityPct + "%";

    templateMeta.textContent =
        templateCfg.uploaded_file
            ? `✓ ${templateCfg.uploaded_file}`
            : "";

    templateDeleteUpload.style.display =
        mode === "upload" && templateCfg.uploaded_file
            ? "block"
            : "none";
}


async function patchTemplate(patch) {
    if (!currentJobId) {
        return;
    }

    try {
        const res = await fetch(
            `/api/jobs/${currentJobId}/template/config`,
            {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(patch || {}),
            }
        );

        const data = await res.json();

        if (!res.ok) {
            throw new Error(
                data.detail || "Không lưu được template"
            );
        }

        Object.assign(templateCfg, data);
    } catch (e) {
        alert(e.message || e);
        await loadTemplateState();
        return;
    }

    syncTemplateControls();
    renderTemplateOverlay();
    refreshPreviewStatus();
}


async function loadSystemTemplates() {
    try {
        const res = await fetch("/api/templates");

        if (!res.ok) {
            return;
        }

        const data = await res.json();
        systemTemplates = data.templates || [];

        templateSelect.innerHTML = "";

        for (const tpl of systemTemplates) {
            const option =
                document.createElement("option");

            option.value = tpl.id;
            option.textContent = tpl.name || tpl.id;

            templateSelect.appendChild(option);
        }
    } catch (_) {
        // keep select empty; overlay still works via direct URLs
    }

    syncTemplateControls();
}


async function loadTemplateState() {
    if (!currentJobId) {
        return;
    }

    try {
        const res = await fetch(
            `/api/jobs/${currentJobId}/template`,
            {cache: "no-store"}
        );

        if (!res.ok) {
            return;
        }

        const data = await res.json();
        Object.assign(templateCfg, data);
    } catch (_) {
        return;
    }

    syncTemplateControls();
    renderTemplateOverlay();
    refreshPreviewStatus();
}


document.querySelectorAll(
    'input[name="templateMode"]'
).forEach((radio) => {
    radio.addEventListener(
        "change",
        () => {
            patchTemplate({mode: getTemplateMode()});
        }
    );
});


templateSelect.addEventListener(
    "change",
    () => {
        patchTemplate({system_template: templateSelect.value});
    }
);


templateOpacity.addEventListener(
    "input",
    () => {
        templateCfg.opacity =
            Number(templateOpacity.value) / 100;

        templateOpacityValue.textContent =
            `${templateOpacity.value}%`;

        renderTemplateOverlay();
    }
);

templateOpacity.addEventListener(
    "change",
    () => {
        patchTemplate({
            opacity:
                Number(templateOpacity.value) / 100,
        });
    }
);


templateInput.addEventListener(
    "change",
    async () => {
        const file = templateInput.files[0];

        if (!file || !currentJobId) {
            return;
        }

        const form = new FormData();
        form.append("template", file);

        try {
            const res = await fetch(
                `/api/jobs/${currentJobId}/template/upload`,
                {method: "POST", body: form}
            );

            const data = await res.json();

            if (!res.ok) {
                throw new Error(
                    data.detail || "Upload template thất bại"
                );
            }

            Object.assign(templateCfg, data);

            templateInput.value = "";

            syncTemplateControls();
            renderTemplateOverlay();
            refreshPreviewStatus();
        } catch (e) {
            alert(e.message || e);
        }
    }
);


templateDeleteUpload.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        if (!confirm("Xóa template đã tải?")) {
            return;
        }

        try {
            const res = await fetch(
                `/api/jobs/${currentJobId}/template/upload`,
                {method: "DELETE"}
            );

            const data = await res.json();

            if (!res.ok) {
                throw new Error(
                    data.detail || "Không xóa được template"
                );
            }

            Object.assign(templateCfg, data);
        } catch (e) {
            alert(e.message || e);
        }

        syncTemplateControls();
        renderTemplateOverlay();
        refreshPreviewStatus();
    }
);


document.getElementById("templateReset")?.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        try {
            const res = await fetch(
                `/api/jobs/${currentJobId}/template/reset`,
                {method: "POST"}
            );

            const data = await res.json();

            if (!res.ok) {
                throw new Error(
                    data.detail || "Không reset được template"
                );
            }

            Object.assign(templateCfg, data);
        } catch (e) {
            alert(e.message || e);
        }

        syncTemplateControls();
        renderTemplateOverlay();
        refreshPreviewStatus();
    }
);


// ======================================
// LOGO / WATERMARK
// ======================================

const logoEnabled =
    document.getElementById("logoEnabled");

const logoInput =
    document.getElementById("logoInput");

const logoMeta =
    document.getElementById("logoMeta");

const logoSize =
    document.getElementById("logoSize");

const logoSizeValue =
    document.getElementById("logoSizeValue");

const logoOpacity =
    document.getElementById("logoOpacity");

const logoOpacityValue =
    document.getElementById("logoOpacityValue");

let logoCfg = {
    enabled: false,
    uploaded: false,
    file_name: null,
    x_norm: 0.85,
    y_norm: 0.03,
    width_norm: 0.12,
    opacity: 0.85,
    aspect_ratio: null,
};

let logoOverlayEl = null;
let logoDrag = null;
let logoMoved = false;
const logoPointers = new Map();
let logoPinchStart = null;
let logoEditMode = false;

const logoEditBtn = document.getElementById("logoEditBtn");
const logoEditBar = document.getElementById("logoEditBar");
const logoEditDone = document.getElementById("logoEditDone");


function enterLogoEditMode() {
    if (logoEditMode) return;
    logoEditMode = true;

    const wrapper = document.getElementById("videoPreviewWrapper");

    // Pause video & disable native controls (iOS Safari steals touch)
    baseVideo.pause();
    baseVideo.controls = false;
    baseVideo.removeAttribute("controls");
    baseVideo.style.pointerEvents = "none";

    // Enable logo interaction
    if (logoOverlayEl) {
        logoOverlayEl.style.pointerEvents = "auto";
        logoOverlayEl.style.touchAction = "none";
        logoOverlayEl.style.zIndex = "50";
    }

    wrapper.classList.add("logo-editing");

    // UI updates
    logoEditBtn.style.display = "none";
    logoEditBar.classList.add("active");

    console.log("[LOGO_EDIT] enterLogoEditMode");
}


function exitLogoEditMode() {
    if (!logoEditMode) return;
    logoEditMode = false;

    const wrapper = document.getElementById("videoPreviewWrapper");

    // Restore video controls
    baseVideo.style.pointerEvents = "";
    baseVideo.controls = true;
    baseVideo.setAttribute("controls", "");

    // Logo overlay stays visible but non-interactive
    if (logoOverlayEl) {
        logoOverlayEl.style.pointerEvents = "none";
        logoOverlayEl.style.zIndex = "9";
    }

    wrapper.classList.remove("logo-editing");

    // UI updates
    logoEditBtn.style.display = "";
    logoEditBar.classList.remove("active");

    console.log("[LOGO_EDIT] exitLogoEditMode");
}


// Hook up buttons
logoEditBtn?.addEventListener("click", () => {
    if (logoCfg.enabled && logoCfg.uploaded) {
        enterLogoEditMode();
    }
});

logoEditDone?.addEventListener("click", () => {
    exitLogoEditMode();
});

function logoVideoDims() {
    return {
        w: baseVideo.videoWidth || 1920,
        h: baseVideo.videoHeight || 1080,
    };
}


function logoAspect() {
    const a = Number(logoCfg.aspect_ratio);
    return (
        Number.isFinite(a) && a > 0
    ) ? a : 1;
}


function logoHeightNorm(wNorm) {
    const dims = logoVideoDims();
    return (
        wNorm * dims.w / logoAspect()
    ) / dims.h;
}


function clampLogoXY() {
    const w = Math.min(
        0.40,
        Math.max(0.03, logoCfg.width_norm)
    );

    logoCfg.width_norm = w;

    const hN = logoHeightNorm(w);

    logoCfg.x_norm = Math.min(
        1 - w,
        Math.max(0, logoCfg.x_norm)
    );

    logoCfg.y_norm = Math.min(
        Math.max(0, 1 - hN),
        Math.max(0, logoCfg.y_norm)
    );
}


let logoGesturesAttached = false;
let logoCurrentCacheBuster = "";
function renderLogoOverlay() {
    logoOverlayEl = document.getElementById("logoOverlay");
    if (!logoOverlayEl) return;

    if (!logoGesturesAttached) {
        attachLogoGestures();
        logoGesturesAttached = true;
    }

    const show = logoCfg.enabled && logoCfg.uploaded;
    logoOverlayEl.style.display = show ? "block" : "none";

    // pointer-events: auto only in edit mode, otherwise none
    logoOverlayEl.style.pointerEvents =
        (show && logoEditMode) ? "auto" : "none";

    // Enable/disable the edit button
    if (logoEditBtn) {
        logoEditBtn.disabled = !show;
    }

    if (!show) {
        // Exit edit mode if logo was hidden
        if (logoEditMode) exitLogoEditMode();
        return;
    }

    logoOverlayEl.style.left = (logoCfg.x_norm * 100) + "%";
    logoOverlayEl.style.top = (logoCfg.y_norm * 100) + "%";
    logoOverlayEl.style.width = (logoCfg.width_norm * 100) + "%";
    logoOverlayEl.style.aspectRatio = logoAspect() + " / 1";
    logoOverlayEl.style.opacity = String(logoCfg.opacity);

    if (logoOverlayEl.dataset.job !== String(currentJobId)) {
        logoOverlayEl.dataset.job = String(currentJobId);
        if (!logoCurrentCacheBuster) logoCurrentCacheBuster = Date.now();
        const src = `/api/jobs/${currentJobId}/logo/file?v=${logoCurrentCacheBuster}`;
        const tempImg = new Image();
        tempImg.onload = () => { logoOverlayEl.src = src; };
        tempImg.onerror = (e) => { console.error("LOGO_TEMP_IMG_ERROR", src, e); };
        logoOverlayEl.onerror = (e) => { console.error("LOGO_OVERLAY_IMG_ERROR", src, e); };
        tempImg.src = src;
    }
}


function syncLogoControls() {
    logoEnabled.checked = !!logoCfg.enabled;

    logoSize.value = Math.round(
        logoCfg.width_norm * 100
    );

    logoSizeValue.textContent =
        Math.round(logoCfg.width_norm * 100) + "%";

    logoOpacity.value = Math.round(
        logoCfg.opacity * 100
    );

    logoOpacityValue.textContent =
        Math.round(logoCfg.opacity * 100) + "%";

    logoMeta.textContent =
        logoCfg.uploaded && logoCfg.file_name
            ? `✓ ${logoCfg.file_name}`
            : "";
}


function syncLogoLabels() {
    logoSizeValue.textContent =
        Math.round(logoCfg.width_norm * 100) + "%";

    logoOpacityValue.textContent =
        Math.round(logoCfg.opacity * 100) + "%";
}


async function patchLogo(patch) {
    if (!currentJobId) {
        return;
    }

    try {
        const res = await fetch(
            `/api/jobs/${currentJobId}/logo/config`,
            {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(patch || {}),
            }
        );

        const data = await res.json();

        if (!res.ok) {
            throw new Error(
                data.detail || "Không lưu được logo"
            );
        }

        Object.assign(logoCfg, data);
    } catch (e) {
        alert(e.message || e);
        await loadLogoState();
        return;
    }

    syncLogoControls();
    renderLogoOverlay();
    refreshPreviewStatus();
}


async function loadLogoState() {
    if (!currentJobId) {
        return;
    }

    try {
        const res = await fetch(
            `/api/jobs/${currentJobId}/logo`,
            {cache: "no-store"}
        );

        if (!res.ok) {
            return;
        }

        const data = await res.json();
        Object.assign(logoCfg, data);
    } catch (_) {
        return;
    }

    syncLogoControls();
    renderLogoOverlay();
    refreshPreviewStatus();
}


function attachLogoGestures() {
    const el = logoOverlayEl;

    el.addEventListener(
        "pointerdown",
        (e) => {
            console.log("[LOGO_EVENT] logoOverlay pointerdown",
                "editMode=", logoEditMode,
                "type=", e.pointerType,
                "id=", e.pointerId);

            if (
                !logoCfg.enabled ||
                !logoCfg.uploaded
            ) {
                return;
            }

            if (!logoEditMode) {
                return;
            }

            e.preventDefault();
            e.stopPropagation();

            try {
                el.setPointerCapture(e.pointerId);
            } catch(_) { console.error(_); }

            logoPointers.set(e.pointerId, {
                x: e.clientX,
                y: e.clientY,
            });

            if (logoPointers.size === 1) {
                logoDrag = {
                    px: e.clientX,
                    py: e.clientY,
                    x0: logoCfg.x_norm,
                    y0: logoCfg.y_norm,
                };

                logoPinchStart = null;
            } else if (logoPointers.size === 2) {
                const pts = [
                    ...logoPointers.values()
                ];

                logoPinchStart = {
                    dist: Math.hypot(
                        pts[0].x - pts[1].x,
                        pts[0].y - pts[1].y
                    ),
                    w0: logoCfg.width_norm,
                };

                logoDrag = null;
            }
        }
    );

    el.addEventListener(
        "pointermove",
        (e) => {
            if (!logoPointers.has(e.pointerId)) {
                return;
            }

            e.preventDefault();
            e.stopPropagation();

            logoPointers.set(e.pointerId, {
                x: e.clientX,
                y: e.clientY,
            });

            if (
                logoPointers.size === 2 &&
                logoPinchStart
            ) {
                const pts = [
                    ...logoPointers.values()
                ];

                const dist = Math.hypot(
                    pts[0].x - pts[1].x,
                    pts[0].y - pts[1].y
                );

                if (
                    logoPinchStart.dist > 0 &&
                    dist > 0
                ) {
                    logoCfg.width_norm =
                        Math.min(
                            0.40,
                            Math.max(
                                0.03,
                                logoPinchStart.w0 *
                                dist /
                                logoPinchStart.dist
                            )
                        );

                    clampLogoXY();
                    renderLogoOverlay();
                    syncLogoLabels();
                    logoMoved = true;
                }

                return;
            }

            if (
                logoPointers.size === 1 &&
                logoDrag
            ) {
                const rect =
                    document.getElementById("videoPreviewWrapper").getBoundingClientRect();

                if (
                    rect.width <= 0 ||
                    rect.height <= 0
                ) {
                    return;
                }

                logoCfg.x_norm =
                    logoDrag.x0 +
                    (e.clientX - logoDrag.px) /
                    rect.width;

                logoCfg.y_norm =
                    logoDrag.y0 +
                    (e.clientY - logoDrag.py) /
                    rect.height;

                clampLogoXY();
                renderLogoOverlay();
                logoMoved = true;
            }
        }
    );

    const endPointer = (e) => {
        console.log("[LOGO_EVENT] logoOverlay pointerup/cancel",
            "type=", e.type,
            "id=", e.pointerId);

        logoPointers.delete(e.pointerId);

        if (logoPointers.size < 2) {
            logoPinchStart = null;
        }

        if (logoPointers.size === 0) {
            logoDrag = null;

            if (logoMoved) {
                logoMoved = false;
                patchLogo({
                    x_norm: logoCfg.x_norm,
                    y_norm: logoCfg.y_norm,
                    width_norm: logoCfg.width_norm,
                });
            }
        }
    };

    el.addEventListener("pointerup", endPointer);
    el.addEventListener("pointercancel", endPointer);

    // Audit: log touch delivery on video & wrapper
    baseVideo.addEventListener("pointerdown", (e) => {
        console.log("[LOGO_EVENT] baseVideo pointerdown",
            "type=", e.pointerType, "id=", e.pointerId);
    });
    document.getElementById("videoPreviewWrapper")
        .addEventListener("pointerdown", (e) => {
            console.log("[LOGO_EVENT] videoPreviewWrapper pointerdown",
                "type=", e.pointerType, "id=", e.pointerId);
        });
}


function logoPreset(name) {
    const m = 0.03;
    const w = logoCfg.width_norm;
    const hN = logoHeightNorm(w);

    const presets = {
        "top-left": {
            x_norm: m,
            y_norm: m,
        },
        "top-right": {
            x_norm: 1 - m - w,
            y_norm: m,
        },
        "bottom-left": {
            x_norm: m,
            y_norm: 1 - m - hN,
        },
        "bottom-right": {
            x_norm: 1 - m - w,
            y_norm: 1 - m - hN,
        },
        "center": {
            x_norm: (1 - w) / 2,
            y_norm: (1 - hN) / 2,
        },
    };

    return presets[name] || null;
}


document.querySelectorAll(
    "#logoCard [data-preset]"
).forEach((btn) => {
    btn.addEventListener("click", () => {
        const pos = logoPreset(btn.dataset.preset);

        if (pos) {
            patchLogo(pos);
        }
    });
});


logoEnabled.addEventListener(
    "change",
    () => {
        const want = logoEnabled.checked;

        if (want && !logoCfg.uploaded) {
            logoEnabled.checked = false;
            alert("Hãy chọn file logo trước.");
            return;
        }

        patchLogo({enabled: want});
    }
);


logoInput.addEventListener(
    "change",
    async () => {
        const file = logoInput.files[0];

        if (!file || !currentJobId) {
            return;
        }

        const form = new FormData();
        form.append("logo", file);

        try {
            const res = await fetch(
                `/api/jobs/${currentJobId}/logo/upload`,
                {method: "POST", body: form}
            );

            const data = await res.json();

            if (!res.ok) {
                throw new Error(
                    data.detail || "Upload logo thất bại"
                );
            }

            Object.assign(logoCfg, data);

            logoCurrentCacheBuster = Date.now();
            if (logoOverlayEl) {
                logoOverlayEl.dataset.job = "";
            }
            syncLogoControls();
            const src = `/api/jobs/${currentJobId}/logo/file?v=${logoCurrentCacheBuster}`;
            const temp = new Image();
            temp.src = src;
            try {
                await temp.decode();
            } catch (err) {
                console.error("LOGO_DECODE_FAILED", src, err);
                throw err;
            }
            renderLogoOverlay();
            enterLogoEditMode();
        } catch (e) {
            alert(e.message || e);
        }
    }
);


logoSize.addEventListener(
    "input",
    () => {
        logoCfg.width_norm =
            Number(logoSize.value) / 100;

        clampLogoXY();
        renderLogoOverlay();
        syncLogoLabels();
    }
);

logoSize.addEventListener(
    "change",
    () => {
        patchLogo({
            width_norm: logoCfg.width_norm,
            x_norm: logoCfg.x_norm,
            y_norm: logoCfg.y_norm,
        });
    }
);


logoOpacity.addEventListener(
    "input",
    () => {
        logoCfg.opacity =
            Number(logoOpacity.value) / 100;

        renderLogoOverlay();
        syncLogoLabels();
    }
);

logoOpacity.addEventListener(
    "change",
    () => {
        patchLogo({opacity: logoCfg.opacity});
    }
);


document.getElementById("logoReset")?.addEventListener(
    "click",
    () => {
        const pos = logoPreset("top-right") || {};

        patchLogo({
            enabled: true,
            width_norm: 0.12,
            opacity: 0.85,
            ...pos,
        });
    }
);


document.getElementById("logoDelete")?.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        if (!confirm("Xóa logo?")) {
            return;
        }

        try {
            await fetch(
                `/api/jobs/${currentJobId}/logo`,
                {method: "DELETE"}
            );
        } catch(_) { console.error(_); }

        logoCfg.uploaded = false;
        logoCfg.enabled = false;
        logoCfg.file_name = null;
        logoInput.value = "";

        exitLogoEditMode();
        syncLogoControls();
        renderLogoOverlay();
    }
);


// ======================================
// FINAL PREVIEW 15-30s
// ======================================

const previewButton =
    document.getElementById("previewButton");

const previewCancelButton =
    document.getElementById("previewCancelButton");

const previewBar =
    document.getElementById("previewBar");

const previewStatus =
    document.getElementById("previewStatus");

const previewVideo =
    document.getElementById("previewVideo");

const previewPlayerBox =
    document.getElementById("previewPlayerBox");

const previewContinueBox =
    document.getElementById("previewContinueBox");

function setPreviewContinueVisible(visible) {
    if (!previewContinueBox) {
        return;
    }

    previewContinueBox.style.display =
        visible ? "block" : "none";
}

function hidePreviewContinue() {
    setPreviewContinueVisible(false);
}

const previewStale =
    document.getElementById("previewStale");

const previewStartInput =
    document.getElementById("previewStartInput");

let previewPolling = false;


function getPreviewDuration() {
    const checked =
        document.querySelector(
            'input[name="previewDuration"]:checked'
        );

    const value = checked
        ? Number(checked.value)
        : 15;

    return value === 30 ? 30 : 15;
}


function parsePreviewStart(text) {
    const raw = String(text || "").trim();

    if (!raw) {
        return 0;
    }

    if (/^\d+(\.\d+)?$/.test(raw)) {
        return Math.max(0, Number(raw));
    }

    const parts = raw.split(":").map(Number);

    if (parts.some((p) => !Number.isFinite(p) || p < 0)) {
        return null;
    }

    let total = 0;

    for (const part of parts) {
        total = total * 60 + part;
    }

    return total;
}


function formatHMS(totalSeconds) {
    const total = Math.max(
        0,
        Math.floor(Number(totalSeconds) || 0)
    );

    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;

    return (
        String(h).padStart(2, "0") + ":" +
        String(m).padStart(2, "0") + ":" +
        String(s).padStart(2, "0")
    );
}


function formatShortDuration(seconds) {
    if (!Number.isFinite(Number(seconds))) {
        return "--";
    }

    const total = Math.max(0, Math.floor(seconds));
    const m = Math.floor(total / 60);
    const s = total % 60;

    return `${m}:${String(s).padStart(2, "0")}`;
}


function baseVideoDuration() {
    const dur = Number(baseVideo.duration);
    return Number.isFinite(dur) && dur > 0 ? dur : null;
}


document.getElementById("previewStartHead")?.addEventListener(
    "click",
    () => {
        previewStartInput.value = "00:00:00";
    }
);

document.getElementById("previewStartMid")?.addEventListener(
    "click",
    () => {
        const dur = baseVideoDuration();

        if (dur === null) {
            previewStartInput.value = "00:00:00";
            return;
        }

        previewStartInput.value = formatHMS(dur / 2);
    }
);

document.getElementById("previewStartTail")?.addEventListener(
    "click",
    () => {
        const dur = baseVideoDuration();
        const previewDur = getPreviewDuration();

        if (dur === null) {
            previewStartInput.value = "00:00:00";
            return;
        }

        previewStartInput.value = formatHMS(
            Math.max(0, dur - previewDur)
        );
    }
);


function setPreviewBusy(busy) {
    previewButton.disabled = busy;
    previewCancelButton.style.display =
        busy ? "block" : "none";
}


async function pollPreview(jobId) {
    previewPolling = true;
    setPreviewBusy(true);

    try {
        for (;;) {
            const response =
                await fetch(
                    `/api/jobs/${jobId}/final-preview/status`,
                    {cache: "no-store"}
                );

            const data = await response.json();

            if (!response.ok) {
                throw new Error(
                    data.detail ||
                    "Không đọc được trạng thái preview"
                );
            }

            const progress =
                Number(data.progress || 0);

            previewBar.style.width =
                `${Math.min(100, progress)}%`;

            let detail =
                `Runtime ${formatShortDuration(data.runtime_seconds)}`;

            if (
                data.rendered_time !== null &&
                data.rendered_time !== undefined &&
                data.duration
            ) {
                detail +=
                    ` • ${formatShortDuration(data.rendered_time)}`
                    + ` / ${formatShortDuration(data.duration)}`;
            }

            if (
                data.eta_seconds !== null &&
                data.eta_seconds !== undefined &&
                data.status === "processing"
            ) {
                detail +=
                    ` • ETA ${formatShortDuration(data.eta_seconds)}`;
            }

            if (data.status === "done") {
                previewBar.style.width = "100%";

                if (data.stale) {
                    previewPlayerBox.style.display =
                        "none";
                    hidePreviewContinue();
                    previewStale.style.display =
                        "block";
                    previewStatus.textContent =
                        "Thiết lập đã thay đổi. Hãy render preview lại.";
                } else {
                    previewVideo.src =
                        `/api/jobs/${jobId}/final-preview/video`
                        + `?v=${Date.now()}`;

                    previewPlayerBox.style.display =
                        "block";
                    setPreviewContinueVisible(true);
                    previewStale.style.display =
                        "none";
                    previewStatus.textContent =
                        `Preview sẵn sàng (${formatHMS(data.start_seconds)}`
                        + ` • ${data.duration}s).`;
                }

                setPreviewBusy(false);
                previewPolling = false;
                return;
            }

            if (data.status === "failed") {
                throw new Error(
                    data.error ||
                    "Preview thất bại"
                );
            }

            if (data.status === "cancelled") {
                previewBar.style.width = "0%";
                previewPlayerBox.style.display = "none";
                hidePreviewContinue();
                previewStatus.textContent = "Đã hủy preview.";
                setPreviewBusy(false);
                previewPolling = false;
                return;
            }

            hidePreviewContinue();
            previewStatus.textContent =
                `${data.message || "Đang render preview..."} ${detail}`;

            await sleep(1000);
        }
    } catch (error) {
        previewBar.style.width = "0%";
        hidePreviewContinue();
        setPreviewBusy(false);
        previewPolling = false;

        showError(
            previewStatus,
            error.message ||
            "Preview thất bại"
        );
    }
}


async function startPreview() {
    clearError(previewStatus);

    if (!currentJobId) {
        showError(
            previewStatus,
            "Không tìm thấy job hiện tại."
        );
        return;
    }

    const duration = getPreviewDuration();
    const start = parsePreviewStart(previewStartInput.value);

    if (start === null) {
        showError(
            previewStatus,
            "Vị trí bắt đầu không hợp lệ (VD: 00:12:30 hoặc 750)."
        );
        return;
    }

    const musicMode = getMusicMode();
    const music = backgroundMusic.files[0];

    if (musicMode === "upload" && !music) {
        // Reuse previously uploaded music if the job has one;
        // the backend returns 400 otherwise.
    }

    setPreviewBusy(true);
    previewPlayerBox.style.display = "none";
    hidePreviewContinue();
    previewStale.style.display = "none";
    previewBar.style.width = "2%";
    previewStatus.textContent = "Đang chuẩn bị preview...";

    const form = new FormData();

    form.append("music_mode", musicMode);

    if (musicMode === "upload" && music) {
        form.append("background_music", music);
    }

    form.append("base_volume", baseVolume.value);
    form.append("music_volume", musicVolume.value);
    form.append("preview_start_seconds", String(start));
    form.append("preview_duration", String(duration));

    try {
        const response = await fetch(
            `/api/jobs/${currentJobId}/final-preview`,
            {method: "POST", body: form}
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail ||
                "Không tạo được preview"
            );
        }

        await pollPreview(currentJobId);
    } catch (error) {
        setPreviewBusy(false);

        showError(
            previewStatus,
            error.message ||
            "Preview thất bại"
        );
    }
}


previewButton.addEventListener("click", startPreview);


previewCancelButton.addEventListener(
    "click",
    async () => {
        if (!currentJobId) {
            return;
        }

        previewCancelButton.disabled = true;

        try {
            await fetch(
                `/api/jobs/${currentJobId}/final-preview/cancel`,
                {method: "POST"}
            );
        } catch (_) {
            // poll loop will surface the state
        }

        previewCancelButton.disabled = false;
    }
);


async function refreshPreviewStatus() {
    if (!currentJobId || previewPolling) {
        return;
    }

    let data = null;

    try {
        const response = await fetch(
            `/api/jobs/${currentJobId}/final-preview/status`,
            {cache: "no-store"}
        );

        if (!response.ok) {
            return;
        }

        data = await response.json();
    } catch (_) {
        return;
    }

    if (!data || data.status === "none") {
        previewPlayerBox.style.display = "none";
        hidePreviewContinue();
        previewStale.style.display = "none";
        previewStatus.textContent = "Chưa có preview.";
        previewBar.style.width = "0%";
        setPreviewBusy(false);
        return;
    }

    if (data.status === "processing") {
        hidePreviewContinue();
        // Resume polling (e.g. page reloaded mid-render).
        pollPreview(currentJobId);
        return;
    }

    if (data.status === "done" && !data.stale) {
        previewVideo.src =
            `/api/jobs/${currentJobId}/final-preview/video`
            + `?v=${Date.now()}`;

        previewPlayerBox.style.display = "block";
        setPreviewContinueVisible(true);
        previewStale.style.display = "none";
        previewStatus.textContent =
            `Preview sẵn sàng (${formatHMS(data.start_seconds)}`
            + ` • ${data.duration}s).`;
        previewBar.style.width = "100%";
        setPreviewBusy(false);
        return;
    }

    if (data.status === "done" && data.stale) {
        previewPlayerBox.style.display = "none";
        hidePreviewContinue();
        previewStale.style.display = "block";
        previewStatus.textContent =
            "Thiết lập đã thay đổi. Hãy render preview lại.";
        setPreviewBusy(false);
        return;
    }

    previewPlayerBox.style.display = "none";
    hidePreviewContinue();
    previewStale.style.display = "none";
    previewStatus.textContent =
        data.status === "failed"
            ? (data.error || "Preview thất bại.")
            : "Chưa có preview.";
    previewBar.style.width = "0%";
    setPreviewBusy(false);
}


async function checkModelStatus() {
    try {
        const response = await fetch('/api/subtitle/runtime-status');
        const data = await response.json();
        
        const colabBadge = document.getElementById('colabStatusBadge');
        const colabText = document.getElementById('colabStatusText');
        const driveBadge = document.getElementById('driveStatusBadge');
        const driveText = document.getElementById('driveStatusText');
        const btn = document.getElementById('btnInstallGipformer');
        
        if (!colabBadge || !driveBadge) return;

        if (data.colab_connected) {
            colabBadge.className = 'badge bg-success';
            colabBadge.textContent = 'Connected';
            colabText.textContent = data.colab_error || 'Phiên Colab đã kết nối';
        } else {
            colabBadge.className = 'badge bg-danger';
            colabBadge.textContent = 'Disconnected';
            colabText.textContent = data.message || 'Colab session chưa kết nối';
        }

        if (data.drive_mounted) {
            driveBadge.className = 'badge bg-success';
            driveBadge.textContent = 'Connected';
            driveText.textContent = data.drive_error || 'Google Drive đã kết nối';
            if (btn) btn.style.display = 'none';
        } else {
            driveBadge.className = 'badge bg-danger';
            driveBadge.textContent = 'Disconnected';
            driveText.textContent = data.message || 'Cần kết nối Drive';
            if (btn) btn.style.display = 'inline-block';
        }
    } catch (e) {
        console.error("Failed to check runtime status", e);
    }
}

document.getElementById('btnInstallGipformer')?.addEventListener('click', async () => {
    const btn = document.getElementById('btnInstallGipformer');
    btn.disabled = true;
    btn.textContent = 'Đang tải...';
    
    const form = new FormData();
    form.append('model_name', 'gipformer1.5-68M-rnnt');
    
    try {
        const res = await fetch('/api/models/install', { method: 'POST', body: form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Lỗi cài đặt model');
        
        // start a polling loop
        const interval = setInterval(async () => {
            const statusRes = await fetch('/api/subtitle/runtime-status');
            const statusData = await statusRes.json();
            if (statusData.model_on_drive || statusData.model_runtime_ready) {
                clearInterval(interval);
                checkModelStatus();
            } else {
                const badge = document.getElementById('gipformerStatusBadge');
                if (badge) badge.textContent = 'Installing...';
            }
        }, 3000);
    } catch(e) {
        alert(e.message);
        btn.disabled = false;
        btn.textContent = 'Tải model vào Drive';
    }
});

// Initial check
refreshSubtitleRuntimeStatus();


let driveAuthInterval = null;

function showDriveAuthError(msg) {
    const box = document.getElementById("driveAuthError");
    if (!box) return;
    box.textContent = msg ? "Lỗi kết nối: " + msg : "";
    box.style.display = msg ? "block" : "none";
}

// Each attempt has a new OAuth URL; confirming before opening *that* URL always fails.
function driveAuthLinkOpened(url) {
    try { return sessionStorage.getItem("driveAuthOpened") === url; } catch (e) { return false; }
}

function syncConfirmAuthButton(data) {
    const btn = document.getElementById("btnConfirmAuth");
    if (!btn) return;
    btn.disabled = data.state !== "waiting_oauth" || !driveAuthLinkOpened(data.oauth_url);
}

document.getElementById("driveAuthLink")?.addEventListener("click", (e) => {
    const url = e.currentTarget.href;
    try { sessionStorage.setItem("driveAuthOpened", url); } catch (err) {}
    const btn = document.getElementById("btnConfirmAuth");
    if (btn) btn.disabled = false;
});

function startDriveAuthPolling() {
    if (driveAuthInterval) clearInterval(driveAuthInterval);
    driveAuthInterval = setInterval(pollDriveAuth, 2000);
    pollDriveAuth();
}

async function pollDriveAuth() {
    try {
        const res = await fetch("/api/colab/drive/status");
        if (!res.ok) return;
        const data = await res.json();

        const colabBadge = document.getElementById("colabStatusBadge");
        const colabText = document.getElementById("colabStatusText");
        const badge = document.getElementById("driveStatusBadge");
        const text = document.getElementById("driveStatusText");
        const btnConnect = document.getElementById("btnConnectDrive");
        const linkContainer = document.getElementById("driveAuthLinkContainer");
        const authLink = document.getElementById("driveAuthLink");

        if (colabBadge && data.colab_connected) {
            colabBadge.textContent = "Connected";
            colabBadge.className = "badge bg-success";
            if (colabText) colabText.textContent = "Phiên Colab đã kết nối";
        }

        if (data.drive_mounted) {
            badge.textContent = "Connected";
            badge.className = "badge bg-success";
            text.textContent = data.message || "Đã kết nối";
            btnConnect.style.display = "none";
            linkContainer.style.display = "none";
            showDriveAuthError(null);
        } else if (data.auth_in_progress) {
            badge.textContent = "Waiting";
            badge.className = "badge bg-warning text-dark";
            text.textContent = data.message || "Đang chờ...";
            btnConnect.style.display = "none";
            showDriveAuthError(null);
            if (data.oauth_url) {
                linkContainer.style.display = "block";
                authLink.href = data.oauth_url;
                syncConfirmAuthButton(data);
            } else {
                linkContainer.style.display = "none";
            }
        } else {
            badge.textContent = "Disconnected";
            badge.className = "badge bg-danger";
            text.textContent = data.message || "Cần kết nối Drive";
            btnConnect.style.display = "block";
            linkContainer.style.display = "none";
            if (data.state === "failed") showDriveAuthError(data.error || data.message);
        }

        if (!data.auth_in_progress && driveAuthInterval) {
            clearInterval(driveAuthInterval);
            driveAuthInterval = null;
            refreshSubtitleRuntimeStatus();
        }
    } catch(e) {
        console.error(e);
    }
}

document.getElementById("btnConnectDrive")?.addEventListener("click", async () => {
    const btn = document.getElementById("btnConnectDrive");
    const label = "Kết nối Google Drive";
    btn.disabled = true;
    btn.textContent = "Đang tạo Colab...";
    showDriveAuthError(null);
    try {
        const res = await fetch("/api/colab/drive/auth/start", {method: "POST"});
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false) {
            throw new Error(data.error || data.detail || ("HTTP " + res.status));
        }
        startDriveAuthPolling();
    } catch(e) {
        console.error("drive auth start failed", e);
        showDriveAuthError(e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = label;
    }
});

document.getElementById("btnConfirmAuth")?.addEventListener("click", async () => {
    const btn = document.getElementById("btnConfirmAuth");
    btn.disabled = true;
    showDriveAuthError(null);
    try {
        const res = await fetch("/api/colab/drive/auth/confirm", {method: "POST"});
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false) {
            throw new Error(data.error || data.detail || ("HTTP " + res.status));
        }
        startDriveAuthPolling();
    } catch(e) {
        console.error("drive auth confirm failed", e);
        showDriveAuthError(e.message);
        btn.disabled = false;
    }
});

document.getElementById("btnCancelAuth")?.addEventListener("click", async () => {
    try {
        await fetch("/api/colab/drive/auth/cancel", {method: "POST"});
        refreshSubtitleRuntimeStatus();
    } catch(e) { console.error(e); }
});


let subEntries = [];
let subMeta = {};
let subLayout = {subtitle_too_dense: false, dense_source_ids: [], offset_ms: 0};
let activeCueIndex = -1;
let activeCueStart = 0;
let subTrack = null;

async function loadSubtitleEditor() {
    if (!currentJobId) return;
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/entries`);
        if (!res.ok) throw new Error("Could not load subtitles");
        const data = await res.json();
        subEntries = data.entries;
        subMeta = data.meta;
        subLayout = data.layout || {subtitle_too_dense: false, dense_source_ids: [], offset_ms: 0};
        renderSubtitleEditor();
        updateSubtitleInfo();
        updateSubOffsetLabel();
        reloadVideoTrack();
        refreshPreviewStatus();
    } catch(e) {
        console.error(e);
    }
}

function updateSubtitleInfo() {
    const info = document.getElementById("subtitleEditorInfo");
    if (!info) return;
    info.innerHTML = `Nguồn: ${subMeta.subtitle_source === 'generated' ? 'Tạo tự động' : 'Upload SRT'}<br>
    Dòng: ${subEntries.length}<br>
    Đã sửa: ${subMeta.edited_count || 0} dòng`;
    
    const btnToggle = document.getElementById("btnToggleSubtitle");
    if (btnToggle) {
        btnToggle.textContent = subMeta.subtitle_enabled ? "TẮT PHỤ ĐỀ" : "BẬT PHỤ ĐỀ";
        btnToggle.className = subMeta.subtitle_enabled ? "action secondary" : "action primary";
    }
}

function reloadVideoTrack() {
    if (!currentJobId) return;
    const v = document.getElementById("baseVideo");
    
    // Remove existing tracks
    const tracks = v.querySelectorAll("track");
    tracks.forEach(t => t.remove());
    
    if (subMeta.subtitle_enabled) {
        const track = document.createElement("track");
        track.kind = "subtitles";
        track.label = "Vietnamese";
        track.srclang = "vi";
        track.src = `/api/jobs/${currentJobId}/subtitle.vtt?v=${Date.now()}`;
        track.default = true;
        v.appendChild(track);
        
        track.onload = () => {
            const textTrack = v.textTracks[0];
            if (textTrack) {
                textTrack.mode = "hidden"; // We will render our own overlay so it's clickable
                        textTrack.oncuechange = () => {
                            const activeCues = textTrack.activeCues;
                            if (activeCues && activeCues.length > 0) {
                                const cue = activeCues[0];
                                // VTT id is "12" or "12.1" (derived child);
                                // both map to source cue #12.
                                activeCueIndex = parseInt(cue.id) - 1;
                                activeCueStart = cue.startTime || 0;
                                highlightCue(activeCueIndex);
                                renderCustomOverlay(cue.text);
                            } else {
                                activeCueIndex = -1;
                                activeCueStart = 0;
                                renderCustomOverlay("");
                            }
                        };
            }
        };
    } else {
        renderCustomOverlay("");
    }
}

function renderCustomOverlay(text) {
    let overlay = document.getElementById("customSubOverlay");
    if (!overlay) {
        const v = document.getElementById("baseVideo");
        const wrap = document.getElementById("videoPreviewWrapper") || v.parentNode;

        overlay = document.createElement("div");
        overlay.id = "customSubOverlay";
        overlay.style.position = "absolute";
        overlay.style.bottom = "10%";
        overlay.style.left = "0";
        overlay.style.width = "100%";
        overlay.style.textAlign = "center";
        overlay.style.whiteSpace = "nowrap";
        overlay.style.color = "white";
        overlay.style.textShadow = "2px 2px 4px #000, -2px -2px 4px #000, 2px -2px 4px #000, -2px 2px 4px #000";
        overlay.style.fontSize = "24px";
        overlay.style.fontWeight = "bold";
        overlay.style.pointerEvents = "auto";
        overlay.style.cursor = "pointer";
        overlay.style.zIndex = "10";
        overlay.style.padding = "0 20px";
        overlay.style.boxSizing = "border-box";
        wrap.style.position = "relative";
        wrap.appendChild(overlay);
        
        overlay.addEventListener("click", () => {
            if (activeCueIndex >= 0) {
                const v = document.getElementById("baseVideo");
                v.pause();
                // Seek to the derived fragment start, edit the SOURCE cue.
                openSubtitleEditor(activeCueIndex, activeCueStart);
            }
        });
    }
    overlay.innerHTML = text.replace(/\n/g, "<br>");
}

function renderSubtitleEditor() {
    const container = document.getElementById("subEntriesContainer");
    if (!container) return;
    
    const filterText = (document.getElementById("subSearchInput")?.value || "").toLowerCase();

    let html = "";

    const denseIds = subLayout.dense_source_ids || [];
    if (subLayout.subtitle_too_dense) {
        html += `
        <div style="margin-bottom: 12px; background: #5a3b00; border: 1px solid #e3b341; padding: 10px 12px; border-radius: 6px; font-size: 13px; color: #ffd97a;">
            ⚠ Phụ đề này quá dài so với thời gian hiển thị.
        </div>
        `;
    }

    subEntries.forEach((entry, i) => {
        if (filterText && !entry.text.toLowerCase().includes(filterText)) return;

        const isDense = denseIds.includes(entry.index);
        const denseBadge = isDense
            ? `<div style="margin-top:6px; font-size:12px; color:#ffd97a;">⚠ Quá dài so với thời gian hiển thị</div>`
            : "";
        html += `
        <div id="sub-entry-${i}" style="margin-bottom: 12px; background: #2d333b; padding: 12px; border-radius: 6px; border-left: 4px solid ${entry.edited ? '#e3b341' : '#444c56'};">
            <div style="display:flex; justify-content:space-between; margin-bottom: 8px; font-size:12px; color:#aaa;">
                <span>#${entry.index} | ${entry.start_text} &rarr; ${entry.end_text}</span>
                <div>
                    <button onclick="playCue(${i})" style="background:transparent; border:none; color:#58a6ff; cursor:pointer; min-height:44px; padding:0 8px;">&#9654; Xem đoạn này</button>
                    <button onclick="restoreEntry(${i})" style="background:transparent; border:none; color:#dc3545; cursor:pointer; margin-left:8px; min-width:44px; min-height:44px;" title="Khôi phục gốc">&#8634;</button>
                </div>
            </div>
            <textarea id="sub-text-${i}" style="width:100%; min-height: 60px; background: #22272e; color: #fff; border: 1px solid #444c56; padding: 8px; border-radius: 4px; resize: vertical;" onblur="saveEntry(${i})">${entry.text}</textarea>
            ${denseBadge}
        </div>
        `;
    });
    container.innerHTML = html;
}

window.playCue = (idx) => {
    const v = document.getElementById("baseVideo");
    v.currentTime = subEntries[idx].start_ms / 1000;
    v.play();
};

window.saveEntry = async (idx) => {
    const newText = document.getElementById(`sub-text-${idx}`).value;
    if (newText === subEntries[idx].text) return; // No change
    
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/entries/${idx + 1}`, {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({text: newText})
        });
        if (res.ok) {
            // Full reload: entries + meta + layout + preview track.
            // Layout re-derives (split may collapse back to 1 cue).
            await loadSubtitleEditor();
        }
    } catch(e) {
        console.error(e);
    }
};

window.restoreEntry = async (idx) => {
    try {
        const form = new FormData();
        form.append("index", idx + 1);
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/restore`, {
            method: "POST",
            body: form
        });
        if (res.ok) {
            await loadSubtitleEditor();
        }
    } catch(e) {
        console.error(e);
    }
};

function highlightCue(idx) {
    const el = document.getElementById(`sub-entry-${idx}`);
    if (el && document.getElementById("subtitleEditorModal").style.display !== "none") {
        el.scrollIntoView({behavior: "smooth", block: "center"});
        document.querySelectorAll("[id^='sub-entry-']").forEach(e => e.style.boxShadow = "none");
        el.style.boxShadow = "0 0 0 2px #58a6ff";
    }
}

function openSubtitleEditor(focusIdx = -1, seekSec = null) {
    document.getElementById("subtitleEditorModal").style.display = "flex";
    renderSubtitleEditor();
    if (seekSec !== null && Number.isFinite(seekSec)) {
        const v = document.getElementById("baseVideo");
        try { v.currentTime = Math.max(0, seekSec); } catch(_) { console.error(_); }
    }
    if (focusIdx >= 0) {
        setTimeout(() => {
            highlightCue(focusIdx);
            document.getElementById(`sub-text-${focusIdx}`)?.focus();
        }, 100);
    }
}

document.getElementById("btnEditSubtitle")?.addEventListener("click", () => openSubtitleEditor());
document.getElementById("btnCloseEditor")?.addEventListener("click", () => {
    document.getElementById("subtitleEditorModal").style.display = "none";
});
document.getElementById("subSearchInput")?.addEventListener("input", renderSubtitleEditor);

document.getElementById("btnReplaceAll")?.addEventListener("click", async () => {
    const findText = document.getElementById("subSearchInput").value;
    const replaceText = document.getElementById("subReplaceInput").value;
    if (!findText) return;
    
    let count = 0;
    subEntries.forEach(e => {
        if (e.text.includes(findText)) count++;
    });
    
    if (count === 0) {
        alert("Không tìm thấy kết quả nào.");
        return;
    }
    
    if (confirm(`Tìm thấy ${count} dòng. Bạn có chắc muốn thay thế tất cả?`)) {
        for (let i = 0; i < subEntries.length; i++) {
            if (subEntries[i].text.includes(findText)) {
                const newText = subEntries[i].text.split(findText).join(replaceText);
                document.getElementById(`sub-text-${i}`).value = newText;
                await saveEntry(i);
            }
        }
    }
});

function updateSubOffsetLabel() {
    const label = document.getElementById("subOffsetLabel");
    if (!label) return;
    const ms = Number(subLayout.offset_ms || 0);
    const sign = ms > 0 ? "+" : "";
    label.textContent = `(${(sign + (ms / 1000).toFixed(1))}s)`;
    const input = document.getElementById("subOffsetInput");
    if (input && document.activeElement !== input) {
        input.placeholder = `ms, hiện tại ${ms}`;
    }
}


async function setSubOffset(ms) {
    if (!currentJobId) return;
    const value = Math.round(Number(ms));
    if (!Number.isFinite(value)) {
        alert("Offset phải là số (ms).");
        return;
    }
    const form = new FormData();
    form.append("offset_ms", String(value));
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/offset`, {
            method: "POST",
            body: form
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Không đặt được offset");
        }
        await loadSubtitleEditor();
    } catch(e) {
        alert("Lỗi offset: " + (e.message || e));
    }
}


document.getElementById("btnOffsetMinus")?.addEventListener("click", () => {
    setSubOffset(Number(subLayout.offset_ms || 0) - 500);
});
document.getElementById("btnOffsetZero")?.addEventListener("click", () => {
    setSubOffset(0);
});
document.getElementById("btnOffsetPlus")?.addEventListener("click", () => {
    setSubOffset(Number(subLayout.offset_ms || 0) + 500);
});
document.getElementById("btnOffsetReset")?.addEventListener("click", () => {
    setSubOffset(0);
});
document.getElementById("btnOffsetApply")?.addEventListener("click", () => {
    const raw = document.getElementById("subOffsetInput")?.value;
    setSubOffset(raw);
});


document.getElementById("btnRestoreAll")?.addEventListener("click", async () => {
    if (confirm("Tất cả chỉnh sửa thủ công sẽ bị mất. Bạn có chắc muốn khôi phục?")) {
        try {
            const form = new FormData();
            const res = await fetch(`/api/jobs/${currentJobId}/subtitle/restore`, {method: "POST", body: form});
            if (res.ok) await loadSubtitleEditor();
        } catch(e) { console.error(e); }
    }
});

document.getElementById("btnToggleSubtitle")?.addEventListener("click", async () => {
    try {
        const form = new FormData();
        form.append("mode", subMeta.subtitle_enabled ? "none" : (subMeta.subtitle_mode || "auto"));
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/mode`, {method: "POST", body: form});
        if (res.ok) await loadSubtitleEditor();
    } catch(e) { console.error(e); }
});

document.getElementById("replaceSrtInput")?.addEventListener("change", async (e) => {
    if (!currentJobId) return;
    const file = e.target.files[0];
    if (!file) return;
    
    if (subMeta.edited_count > 0 && !confirm("Phụ đề hiện tại đã được chỉnh sửa. Bạn có chắc muốn thay file mới?")) {
        e.target.value = "";
        return;
    }
    
    const form = new FormData();
    form.append("file", file);
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/upload`, {method: "POST", body: form});
        if (res.ok) await loadSubtitleEditor();
    } catch(err) {
        alert("Lỗi upload: " + err.message);
    }
    e.target.value = "";
});

// Update the pollSubtitle logic to load subtitle editor when done


document.querySelectorAll('input[name="subtitleMode"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        const mode = e.target.value;
        const colabPanel = document.getElementById("subtitleColabPanel");
        const uploadPanel = document.getElementById("subtitleUploadPanel");
        
        if (mode === "colab") {
            colabPanel.style.display = "block";
            if (uploadPanel) uploadPanel.style.display = "none";
        } else if (mode === "upload") {
            colabPanel.style.display = "none";
            if (uploadPanel) uploadPanel.style.display = "block";
        } else {
            colabPanel.style.display = "none";
            if (uploadPanel) uploadPanel.style.display = "none";
            
            // disable subtitle in meta
            if (currentJobId) {
                const form = new FormData();
                form.append("mode", "none");
                fetch(`/api/jobs/${currentJobId}/subtitle/mode`, {method: "POST", body: form})
                .then(() => loadSubtitleEditor());
            }
        }
    });
});

// Setup upload panel HTML if not exists


document.getElementById("btnInitialUploadSrt")?.addEventListener("click", async () => {
    if (!currentJobId) { alert("Vui lòng tạo video V1 trước"); return; }
    const file = document.getElementById("initialSrtInput").files[0];
    if (!file) { alert("Chưa chọn file"); return; }
    
    document.getElementById("btnInitialUploadSrt").disabled = true;
    const form = new FormData();
    form.append("file", file);
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/upload`, {method: "POST", body: form});
        if (res.ok) {
            document.getElementById("subtitleResult").style.display = "block";
            document.getElementById("subtitleResultMeta").textContent = "Upload thành công";
            await loadSubtitleEditor();
        } else {
            alert("Upload thất bại");
        }
    } catch(err) {
        alert("Lỗi upload: " + err.message);
    }
    document.getElementById("btnInitialUploadSrt").disabled = false;
});

// Start polling unified status
setInterval(refreshSubtitleRuntimeStatus, 5000);
refreshSubtitleRuntimeStatus();

// Note: pollDriveAuth is still used by auth buttons below, but regular status display uses refreshSubtitleRuntimeStatus

// Modify startSubtitle to catch DRIVE_AUTH_REQUIRED
// We already have showSubtitleError, we just need to catch the exact error response

</script>

<div id="subtitleEditorModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:9999; flex-direction:column; padding: 12px; box-sizing: border-box;">
    <div style="background:#1c2128; flex:1; display:flex; flex-direction:column; border-radius: 8px; overflow:hidden; max-width: 800px; margin: 0 auto; width: 100%;">
        <div style="padding: 16px; background: #22272e; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444c56;">
            <h3 style="margin:0;">Chỉnh sửa phụ đề</h3>
            <button id="btnCloseEditor" style="background:transparent; color:#fff; border:none; font-size:24px; cursor:pointer; min-width:44px; min-height:44px;">&times;</button>
        </div>
        
        <div style="padding: 12px; background: #22272e; border-bottom: 1px solid #444c56; display: flex; gap: 8px;">
            <input type="text" id="subSearchInput" placeholder="🔍 Tìm kiếm..." style="flex:1; padding: 8px; border-radius: 4px; border: 1px solid #444c56; background: #2d333b; color: #fff;">
            <input type="text" id="subReplaceInput" placeholder="Thay thế..." style="flex:1; padding: 8px; border-radius: 4px; border: 1px solid #444c56; background: #2d333b; color: #fff;">
            <button id="btnReplaceAll" class="primary" style="padding: 0 12px;">Thay tất cả</button>
        </div>

        <div style="padding: 10px 12px; background: #22272e; border-bottom: 1px solid #444c56;">
            <div style="font-size: 13px; font-weight: 700; margin-bottom: 8px;">
                Đồng bộ phụ đề
                <span id="subOffsetLabel" style="color:#8b949e; font-weight:400;"></span>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <button id="btnOffsetMinus" style="flex:1; min-width:64px; min-height:44px; background:#2d333b; color:#fff; border:1px solid #444c56; border-radius:4px; cursor:pointer;">-0.5s</button>
                <button id="btnOffsetZero" style="flex:1; min-width:64px; min-height:44px; background:#2d333b; color:#fff; border:1px solid #444c56; border-radius:4px; cursor:pointer;">0.0s</button>
                <button id="btnOffsetPlus" style="flex:1; min-width:64px; min-height:44px; background:#2d333b; color:#fff; border:1px solid #444c56; border-radius:4px; cursor:pointer;">+0.5s</button>
                <button id="btnOffsetReset" style="flex:1; min-width:64px; min-height:44px; background:#2d333b; color:#fff; border:1px solid #444c56; border-radius:4px; cursor:pointer;">Reset</button>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; margin-top: 8px;">
                <input type="number" id="subOffsetInput" step="50" placeholder="ms, ví dụ -500" style="flex:1; padding: 8px; border-radius: 4px; border: 1px solid #444c56; background: #2d333b; color: #fff;">
                <button id="btnOffsetApply" class="primary" style="padding: 0 12px; min-height:44px;">Áp dụng</button>
            </div>
        </div>
        
        <div id="subEntriesContainer" style="flex:1; overflow-y:auto; padding: 12px; background: #1c2128;">
            <!-- Entries inserted here -->
        </div>
        
        <div style="padding: 12px; background: #22272e; border-top: 1px solid #444c56; display: flex; justify-content: space-between; align-items: center;">
            <button id="btnRestoreAll" style="background:#dc3545; color:#fff; border:none; padding: 8px 16px; border-radius: 4px; cursor: pointer;">Khôi phục toàn bộ</button>
        </div>
    </div>
</div>

</body>
</html>
"""


# ==========================================================
# UTILITIES
# ==========================================================

def run_command(
    command: list[str],
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def probe_duration(
    path: Path,
) -> float:
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "ffprobe failed"
        )

    value = result.stdout.strip()

    if not value:
        raise RuntimeError(
            "Không đọc được duration"
        )

    duration = float(value)

    if duration <= 0:
        raise RuntimeError(
            "Duration không hợp lệ"
        )

    return duration


def probe_video_codec(
    path: Path,
) -> str:
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    if result.returncode != 0:
        return ""

    return result.stdout.strip().lower()


def format_duration(
    seconds: float | int | None,
) -> str:
    if seconds is None:
        return "00:00:00"

    total = max(
        0,
        int(round(float(seconds)))
    )

    hours = total // 3600
    minutes = (
        total % 3600
    ) // 60
    secs = total % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d}"
    )


def seconds_from_ffmpeg_time(
    value: str,
) -> float:
    parts = value.strip().split(":")

    if len(parts) != 3:
        return 0.0

    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return 0.0

    return (
        hours * 3600
        + minutes * 60
        + seconds
    )


def safe_extension(
    filename: str | None,
    allowed: set[str],
) -> str:
    if not filename:
        raise HTTPException(
            400,
            "Tên file không hợp lệ"
        )

    suffix = Path(
        filename
    ).suffix.lower()

    if suffix not in allowed:
        raise HTTPException(
            400,
            f"Định dạng không hỗ trợ: "
            f"{suffix or 'unknown'}"
        )

    return suffix


def write_json(
    path: Path,
    data: dict,
) -> None:
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    temp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(path)


def read_json(
    path: Path,
) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            str(path)
        )

    return json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )


def save_upload(
    upload: UploadFile,
    destination: Path,
    max_bytes: int | None = None,
) -> None:
    written = 0

    with destination.open(
        "wb"
    ) as output:
        while True:
            chunk = upload.file.read(1024 * 1024)

            if not chunk:
                break

            written += len(chunk)

            if (
                max_bytes is not None
                and written > max_bytes
            ):
                try:
                    output.close()
                except Exception:
                    pass

                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass

                raise HTTPException(
                    413,
                    "File vượt quá dung lượng cho phép",
                )

            output.write(chunk)


def ffmpeg_log_tail(
    path: Path,
    limit: int = 7000,
) -> str:
    if not path.exists():
        return ""

    try:
        text = path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return ""

    return text[-limit:]


# ==========================================================
# GENERIC FFMPEG PROGRESS
# ==========================================================

def run_ffmpeg_progress(
    *,
    command: list[str],
    state_path: Path,
    log_path: Path,
    duration: float,
    progress_start: float,
    progress_end: float,
    stage_name: str,
    pid_file: Path | None = None,
) -> int:

    if duration <= 0:
        raise RuntimeError(
            "Duration không hợp lệ"
        )

    state = read_json(
        state_path
    )

    started_at = time.time()

    state.update({
        "started_at": started_at,
        "status": "processing",
    })

    write_json(
        state_path,
        state,
    )

    last_write = 0.0
    rendered_seconds = 0.0

    # Global cap: one burst of renders can't hang the VPS.
    with FFMPEG_SEMAPHORE:
        return _run_ffmpeg_progress_locked(
            command=command,
            state_path=state_path,
            log_path=log_path,
            duration=duration,
            progress_start=progress_start,
            progress_end=progress_end,
            stage_name=stage_name,
            pid_file=pid_file,
            state=state,
            started_at=started_at,
        )


def _run_ffmpeg_progress_locked(
    *,
    command: list[str],
    state_path: Path,
    log_path: Path,
    duration: float,
    progress_start: float,
    progress_end: float,
    stage_name: str,
    pid_file: Path | None,
    state: dict,
    started_at: float,
) -> int:

    last_write = 0.0
    rendered_seconds = 0.0

    with log_path.open(
        "a",
        encoding="utf-8",
        errors="replace",
    ) as log_file:

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=log_file,
            text=True,
            bufsize=1,
        )

        if pid_file is not None:
            try:
                pid_file.write_text(
                    str(process.pid),
                    encoding="utf-8",
                )
            except OSError:
                pass

        if process.stdout is None:
            process.kill()

            raise RuntimeError(
                "Không đọc được FFmpeg progress"
            )

        for raw_line in process.stdout:

            line = raw_line.strip()

            if not line:
                continue

            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1,
            )

            if key == "out_time":
                rendered_seconds = (
                    seconds_from_ffmpeg_time(
                        value
                    )
                )

            if key == "out_time_us":
                try:
                    rendered_seconds = max(
                        rendered_seconds,
                        float(value)
                        / 1_000_000,
                    )
                except ValueError:
                    pass

            rendered_seconds = max(
                0.0,
                rendered_seconds,
            )

            ratio = min(
                1.0,
                rendered_seconds
                / duration,
            )

            progress = (
                progress_start
                + (
                    progress_end
                    - progress_start
                )
                * ratio
            )

            elapsed = max(
                0.0,
                time.time()
                - started_at,
            )

            eta = None

            if ratio > 0.001:
                eta = max(
                    0.0,
                    elapsed / ratio
                    - elapsed,
                )

            now = time.time()

            if (
                now - last_write
                < 0.5
            ):
                continue

            last_write = now

            state.update({
                "progress": round(
                    progress,
                    1,
                ),
                "rendered_seconds":
                    rendered_seconds,
                "rendered_time_text":
                    format_duration(
                        rendered_seconds
                    ),
                "elapsed_seconds":
                    elapsed,
                "elapsed_text":
                    format_duration(
                        elapsed
                    ),
                "eta_seconds":
                    round(eta, 1)
                    if eta is not None
                    else None,
                "eta_text":
                    format_duration(
                        eta
                    )
                    if eta is not None
                    else None,
                "message":
                    f"{stage_name} "
                    f"{progress:.1f}%",
            })

            write_json(
                state_path,
                state,
            )

        return_code = (
            process.wait()
        )

    return return_code


# ==========================================================
# V1
# ==========================================================

def build_v1_copy_command(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    duration: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",

        "-stream_loop", "-1",
        "-i", str(video_path),

        "-i", str(audio_path),

        "-map", "0:v:0",
        "-map", "1:a:0",

        "-t",
        f"{duration:.6f}",

        "-c:v", "copy",

        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",

        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        "-progress",
        "pipe:1",

        "-nostats",

        str(output_path),
    ]


def build_v1_h264_command(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    duration: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",

        "-stream_loop", "-1",
        "-i", str(video_path),

        "-i", str(audio_path),

        "-map", "0:v:0",
        "-map", "1:a:0",

        "-t",
        f"{duration:.6f}",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",

        "-movflags",
        "+faststart",

        "-progress",
        "pipe:1",

        "-nostats",

        str(output_path),
    ]


def process_v1(
    job_id: str,
) -> None:

    job_dir = (
        require_job_dir(job_id)
    )

    state_path = (
        job_dir / "state.json"
    )

    log_path = (
        job_dir / "ffmpeg-v1.log"
    )

    try:
        state = read_json(
            state_path
        )

        video_path = Path(
            state["video_path"]
        )

        audio_path = Path(
            state["audio_path"]
        )

        output_path = (
            job_dir / "output.mp4"
        )

        started = time.time()

        state.update({
            "status": "processing",
            "progress": 8,
            "message":
                "Đang đọc thông tin media...",
            "started_at": started,
        })

        write_json(
            state_path,
            state,
        )

        video_duration = (
            probe_duration(
                video_path
            )
        )

        audio_duration = (
            probe_duration(
                audio_path
            )
        )

        codec = (
            probe_video_codec(
                video_path
            )
        )

        loops = max(
            1,
            int(
                audio_duration
                / video_duration
            ) + 1,
        )

        state.update({
            "video_duration":
                video_duration,
            "video_duration_text":
                format_duration(
                    video_duration
                ),

            "audio_duration":
                audio_duration,
            "audio_duration_text":
                format_duration(
                    audio_duration
                ),

            "video_codec":
                codec,

            "estimated_loops":
                loops,

            "progress": 20,

            "rendered_time_text":
                "00:00:00",

            "message":
                f"Đang render • "
                f"{loops} vòng loop",
        })

        write_json(
            state_path,
            state,
        )

        command = (
            build_v1_copy_command(
                video_path,
                audio_path,
                output_path,
                audio_duration,
            )
        )

        result = (
            run_ffmpeg_progress(
                command=command,
                state_path=state_path,
                log_path=log_path,
                duration=audio_duration,
                progress_start=20,
                progress_end=98,
                stage_name=
                    "Đang render",
            )
        )

        if result != 0:

            if output_path.exists():
                output_path.unlink()

            state = read_json(
                state_path
            )

            state.update({
                "progress": 20,
                "message":
                    "Đang fallback H.264...",
            })

            write_json(
                state_path,
                state,
            )

            command = (
                build_v1_h264_command(
                    video_path,
                    audio_path,
                    output_path,
                    audio_duration,
                )
            )

            result = (
                run_ffmpeg_progress(
                    command=command,
                    state_path=state_path,
                    log_path=log_path,
                    duration=
                        audio_duration,
                    progress_start=20,
                    progress_end=98,
                    stage_name=
                        "Đang encode H.264",
                )
            )

        if result != 0:
            raise RuntimeError(
                ffmpeg_log_tail(
                    log_path
                )
                or "FFmpeg render failed"
            )

        if not output_path.exists():
            raise RuntimeError(
                "Không tạo được output"
            )

        output_duration = (
            probe_duration(
                output_path
            )
        )

        elapsed = (
            time.time()
            - started
        )

        state = read_json(
            state_path
        )

        state.update({
            "status": "done",
            "progress": 100,

            "message":
                "Hoàn tất.",

            "output_path":
                str(output_path),

            "output_duration":
                output_duration,

            "output_duration_text":
                format_duration(
                    output_duration
                ),

            "output_size":
                output_path.stat().st_size,

            "rendered_time_text":
                format_duration(
                    audio_duration
                ),

            "elapsed_seconds":
                elapsed,

            "elapsed_text":
                format_duration(
                    elapsed
                ),

            "eta_seconds": 0,
            "eta_text":
                "00:00:00",
        })

        write_json(
            state_path,
            state,
        )

    except Exception as exc:

        try:
            state = read_json(
                state_path
            )
        except Exception:
            state = {}

        started = state.get(
            "started_at"
        )

        elapsed = (
            time.time()
            - started
            if started
            else 0
        )

        state.update({
            "status": "failed",
            "progress": 0,

            "message":
                "Render thất bại.",

            "error":
                str(exc),

            "elapsed_text":
                format_duration(
                    elapsed
                ),
        })

        write_json(
            state_path,
            state,
        )


# ==========================================================
# V2 — BACKGROUND MUSIC
# ==========================================================

def process_mix(
    job_id: str,
    background_music: Path | None = None,
    base_volume: float = 100.0,
    music_volume: float = 15.0,
    music_mode: str = "upload",
    music_source: str | None = "upload",
) -> None:

    job_dir = (
        require_job_dir(job_id)
    )

    mix_state_path = (
        job_dir / "mix.json"
    )

    log_path = (
        job_dir / "ffmpeg-mix.log"
    )

    base_video = (
        job_dir / "output.mp4"
    )

    final_video = (
        job_dir / "final.mp4"
    )

    try:

        if not base_video.exists():
            raise RuntimeError(
                "Không tìm thấy video V1"
            )

        started = time.time()

        duration = (
            probe_duration(
                base_video
            )
        )

        subtitle_path = (
            get_subtitle_burn_path(
                job_dir
            )
        )

        # Logo overlay (None when disabled; loud LOGO_NOT_FOUND
        # when enabled but the file is missing/invalid).
        render_logo: RenderLogo | None = (
            get_render_logo(
                job_dir,
                base_video,
            )
        )

        # Template frame (None when mode is none; loud codes
        # TEMPLATE_UPLOAD_REQUIRED / TEMPLATE_NOT_FOUND / ...).
        render_template: TemplateOverlay | None = (
            resolve_template(
                job_dir,
                base_video,
            )
        )

        # Guard: mode none → không nhạc, không probe music.
        has_music = (
            music_mode != "none"
            and background_music is not None
        )

        if has_music:
            music_duration: float | None = (
                probe_duration(
                    background_music
                )
            )

            loops = (
                estimated_music_loops(
                    duration,
                    music_duration,
                )
            )
        else:
            background_music = None
            music_source = None
            music_duration = None
            loops = 0

        if has_music:
            message = (
                "Đang chuẩn bị nhạc nền..."
            )
        elif subtitle_path is not None:
            message = (
                "Đang chuẩn bị render phụ đề..."
            )
        else:
            message = (
                "Đang chuẩn bị render final..."
            )

        state = {
            "status":
                "processing",

            "progress":
                10,

            "message":
                message,

            "music_mode":
                music_mode,

            "music_source":
                music_source
                if has_music
                else None,

            "duration":
                duration,

            "duration_text":
                format_duration(
                    duration
                ),

            "music_duration":
                music_duration,

            "music_duration_text":
                format_duration(
                    music_duration
                )
                if music_duration is not None
                else None,

            "estimated_music_loops":
                loops,

            "base_volume":
                base_volume,

            "music_volume":
                music_volume,

            "subtitle_enabled":
                subtitle_path is not None,

            "logo_enabled":
                render_logo is not None,

            "logo_geometry":
                {
                    "x_px":
                        render_logo.geometry.x_px,
                    "y_px":
                        render_logo.geometry.y_px,
                    "w_px":
                        render_logo.geometry.w_px,
                    "h_px":
                        render_logo.geometry.h_px,
                    "opacity":
                        render_logo.geometry.opacity,
                }
                if render_logo is not None
                else None,

            "template_enabled":
                render_template is not None,

            "template":
                {
                    "path":
                        str(render_template.path),
                    "opacity":
                        render_template.opacity,
                    "width":
                        render_template.width,
                    "height":
                        render_template.height,
                }
                if render_template is not None
                else None,

            "rendered_time_text":
                "00:00:00",

            "started_at":
                started,
        }

        write_json(
            mix_state_path,
            state,
        )

        # Smart render dispatcher: music never decides the video mode.
        # FAST_COPY (no logo/template/subtitle) and DECORATED_LOOP_COPY
        # (static logo/template, no subtitle burn) avoid re-encoding
        # the full-length video. Subtitle burn always needs FULL_ENCODE.
        render_mode = decide_final_render_mode(
            has_logo=render_logo is not None,
            has_template=render_template is not None,
            has_subtitle=subtitle_path is not None,
        )

        state = read_json(mix_state_path)
        state.update({"render_mode": render_mode})
        write_json(mix_state_path, state)

        result: int | None = None

        if render_mode == DECORATED_LOOP_COPY:
            # FAST PATH: decorate the short upload source once, then
            # loop-copy it to the full duration. Any problem falls
            # back to the legacy full encode below (never fails the
            # job outright because of the optimization path).
            try:
                source_video, source_duration = get_short_source(job_dir)
                if source_video is None or source_duration is None:
                    raise RuntimeError("SHORT_SOURCE_NOT_FOUND")

                source_w, source_h = probe_video_dims(source_video)

                cache_key = decorated_loop_cache_key(
                    source_video=source_video,
                    logo_path=(
                        render_logo.path
                        if render_logo is not None
                        else None
                    ),
                    geometry=(
                        render_logo.geometry
                        if render_logo is not None
                        else None
                    ),
                    template=render_template,
                    video_width=source_w,
                    video_height=source_h,
                )

                decorated_path = read_decorated_cache(job_dir, cache_key)

                if decorated_path is None:
                    decorated_path, _ = cache_paths(job_dir)
                    decorated_path.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    if decorated_path.exists():
                        decorated_path.unlink()

                    decorate_cmd = build_decorated_loop_command(
                        source_video=source_video,
                        source_duration=source_duration,
                        logo_path=(
                            render_logo.path
                            if render_logo is not None
                            else None
                        ),
                        geometry=(
                            render_logo.geometry
                            if render_logo is not None
                            else None
                        ),
                        template=render_template,
                        output_path=decorated_path,
                    )

                    decorate_rc = run_ffmpeg_progress(
                        command=decorate_cmd,
                        state_path=mix_state_path,
                        log_path=log_path,
                        duration=source_duration,
                        progress_start=15,
                        progress_end=30,
                        stage_name="Đang chuẩn bị logo/template",
                    )

                    if decorate_rc != 0 or not decorated_path.exists():
                        raise RuntimeError(
                            ffmpeg_log_tail(log_path)
                            or "FAST_PATH_FAILED decorate"
                        )

                    decorated_dur = probe_duration(decorated_path)
                    if abs(decorated_dur - source_duration) > 1.0:
                        raise RuntimeError(
                            "FAST_PATH_FAILED bad loop duration"
                        )

                    write_decorated_cache(
                        job_dir,
                        cache_key,
                        source_duration=source_duration,
                    )

                fast_cmd = build_loop_copy_final_command(
                    loop_video=decorated_path,
                    base_video=base_video,
                    background_music=(
                        background_music if has_music else None
                    ),
                    output_path=final_video,
                    duration=duration,
                    base_volume=base_volume,
                    music_volume=music_volume,
                )

                result = run_ffmpeg_progress(
                    command=fast_cmd,
                    state_path=mix_state_path,
                    log_path=log_path,
                    duration=duration,
                    progress_start=30,
                    progress_end=98,
                    stage_name="Đang ghép video nhanh",
                )

                if result != 0:
                    raise RuntimeError(
                        ffmpeg_log_tail(log_path)
                        or "FAST_PATH_FAILED copy"
                    )
            except Exception as fast_exc:
                with log_path.open(
                    "a", encoding="utf-8", errors="replace"
                ) as fast_log:
                    fast_log.write(
                        "\n[fast-final] FAST_PATH_FAILED"
                        " -> FALLBACK_FULL_ENCODE: "
                        f"{fast_exc}\n"
                    )
                try:
                    if final_video.exists():
                        final_video.unlink()
                except OSError:
                    pass
                state = read_json(mix_state_path)
                state.update({
                    "progress": 15,
                    "message": "Đang render full encode...",
                    "render_mode": FULL_ENCODE,
                    "fast_path_error": str(fast_exc),
                })
                write_json(mix_state_path, state)
                render_mode = FULL_ENCODE
                result = None

        if result is None:
            if render_logo is not None:
                stage_name = (
                    "Đang render full encode"
                    if render_mode == FULL_ENCODE
                    and subtitle_path is not None
                    else "Đang render logo"
                )

                command = (
                    build_final_with_logo_command(
                        base_video=
                            base_video,

                        background_music=
                            background_music
                            if has_music
                            else None,

                        logo_path=
                            render_logo.path,

                        geometry=
                            render_logo.geometry,

                        output_path=
                            final_video,

                        duration=
                            duration,

                        base_volume=
                            base_volume,

                        music_volume=
                            music_volume,

                        subtitle_path=
                            subtitle_path,

                        template=
                            render_template,
                    )
                )
            else:
                if render_mode == FAST_COPY:
                    stage_name = "Đang ghép video nhanh"
                elif has_music:
                    stage_name = "Đang mix nhạc nền"
                elif subtitle_path is not None:
                    stage_name = "Đang render phụ đề"
                else:
                    stage_name = "Đang render final"

                command = (
                    build_mix_command(
                        base_video=
                            base_video,

                        background_music=
                            background_music,

                        output_path=
                            final_video,

                        duration=
                            duration,

                        base_volume=
                            base_volume,

                        music_volume=
                            music_volume,

                        encode_video=(
                            render_template is not None
                        ),

                        subtitle_path=
                            subtitle_path,

                        template=
                            render_template,
                    )
                )

            result = (
                run_ffmpeg_progress(
                    command=command,

                    state_path=
                        mix_state_path,

                    log_path=
                        log_path,

                    duration=
                        duration,

                    progress_start=15,
                    progress_end=98,

                    stage_name=
                        stage_name,
                )
            )

        if result != 0 and render_logo is None:

            if final_video.exists():
                final_video.unlink()

            state = read_json(
                mix_state_path
            )

            state.update({
                "progress": 15,
                "message":
                    "Đang fallback H.264...",
            })

            write_json(
                mix_state_path,
                state,
            )

            if has_music:
                command = (
                    build_mix_command(
                        base_video=
                            base_video,

                        background_music=
                            background_music,

                        output_path=
                            final_video,

                        duration=
                            duration,

                        base_volume=
                            base_volume,

                        music_volume=
                            music_volume,

                        encode_video=True,

                        subtitle_path=
                            subtitle_path,

                        template=
                            render_template,
                    )
                )
            else:
                command = (
                    build_mix_without_music_command(
                        base_video=
                            base_video,

                        output_path=
                            final_video,

                        duration=
                            duration,

                        base_volume=
                            base_volume,

                        subtitle_path=
                            subtitle_path,

                        force_encode_video=True,

                        template=
                            render_template,
                    )
                )

            result = (
                run_ffmpeg_progress(
                    command=command,

                    state_path=
                        mix_state_path,

                    log_path=
                        log_path,

                    duration=
                        duration,

                    progress_start=15,
                    progress_end=98,

                    stage_name=
                        "Đang encode final",
                )
            )

        if result != 0:
            raise RuntimeError(
                ffmpeg_log_tail(
                    log_path
                )
                or "FFmpeg mix failed"
            )

        if not final_video.exists():
            raise RuntimeError(
                "Không tạo được final.mp4"
            )

        final_duration = (
            probe_duration(
                final_video
            )
        )

        elapsed = (
            time.time()
            - started
        )

        state = read_json(
            mix_state_path
        )

        state.update({
            "status": "done",

            "progress": 100,

            "message":
                "Hoàn tất video final.",

            "final_path":
                str(final_video),

            "final_duration":
                final_duration,

            "final_duration_text":
                format_duration(
                    final_duration
                ),

            "final_size":
                final_video.stat().st_size,

            "rendered_time_text":
                format_duration(
                    duration
                ),

            "elapsed_seconds":
                elapsed,

            "elapsed_text":
                format_duration(
                    elapsed
                ),

            "eta_seconds":
                0,

            "eta_text":
                "00:00:00",

            "completed_at":
                time.time(),
        })

        write_json(
            mix_state_path,
            state,
        )

    except Exception as exc:

        try:
            state = read_json(
                mix_state_path
            )
        except Exception:
            state = {}

        started = state.get(
            "started_at"
        )

        elapsed = (
            time.time() - started
            if started
            else 0
        )

        state.update({
            "status": "failed",

            "progress": 0,

            "message":
                "Mix thất bại.",

            "error":
                str(exc),

            "elapsed_text":
                format_duration(
                    elapsed
                ),

            "completed_at":
                time.time(),
        })

        write_json(
            mix_state_path,
            state,
        )


# ==========================================================
# ROUTES
# ==========================================================

@app.get(
    "/",
    response_class=HTMLResponse,
)
def index():
    return HTML


@app.get("/health")
def health():
    return {
        "ok": True,
        "service":
            "loop-video-audio",
        "version":
            "2.0",
    }


@app.post("/api/render")
def create_render(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
):
    video_ext = (
        safe_extension(
            video.filename,
            VIDEO_EXTENSIONS,
        )
    )

    audio_ext = (
        safe_extension(
            audio.filename,
            AUDIO_EXTENSIONS,
        )
    )

    job_id = uuid.uuid4().hex

    job_dir = (
        require_job_dir(job_id)
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    video_path = (
        job_dir
        / f"input_video{video_ext}"
    )

    audio_path = (
        job_dir
        / f"input_audio{audio_ext}"
    )

    try:
        save_upload(
            video,
            video_path,
            MAX_VIDEO_BYTES,
        )

        save_upload(
            audio,
            audio_path,
            MAX_AUDIO_BYTES,
        )

    except HTTPException:
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        raise

    except Exception:

        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

        raise HTTPException(
            500,
            "Không lưu được upload"
        )

    if video_path.stat().st_size <= 0:
        raise HTTPException(
            400,
            "Video rỗng"
        )

    if audio_path.stat().st_size <= 0:
        raise HTTPException(
            400,
            "Audio rỗng"
        )

    state = {
        "job_id":
            job_id,

        "status":
            "queued",

        "progress":
            5,

        "message":
            "Đã nhận file.",

        "video_path":
            str(video_path),

        "audio_path":
            str(audio_path),

        "video_filename":
            video.filename,

        "audio_filename":
            audio.filename,
    }

    write_json(
        job_dir / "state.json",
        state,
    )

    threading.Thread(
        target=process_v1,
        args=(job_id,),
        daemon=True,
    ).start()

    return {
        "job_id": job_id,
        "status": "queued",
    }


@app.get(
    "/api/jobs/{job_id}"
)
def get_job(
    job_id: str,
):
    job_dir = (
        require_job_dir(job_id)
    )

    state_path = (
        job_dir / "state.json"
    )

    if not state_path.exists():
        raise HTTPException(
            404,
            "Job không tồn tại"
        )

    state = read_json(
        state_path
    )

    status = state.get(
        "status"
    )

    elapsed = state.get(
        "elapsed_seconds",
        0,
    )

    started = state.get(
        "started_at"
    )

    if (
        status == "processing"
        and started
    ):
        elapsed = max(
            0,
            time.time()
            - float(started),
        )

    return {
        "job_id":
            job_id,

        "status":
            status,

        "progress":
            state.get(
                "progress",
                0,
            ),

        "message":
            state.get(
                "message"
            ),

        "error":
            state.get(
                "error"
            ),

        "video_duration_text":
            state.get(
                "video_duration_text"
            ),

        "audio_duration_text":
            state.get(
                "audio_duration_text"
            ),

        "output_duration_text":
            state.get(
                "output_duration_text"
            ),

        "rendered_time_text":
            state.get(
                "rendered_time_text"
            ),

        "elapsed_text":
            format_duration(
                elapsed
            ),

        "eta_text":
            state.get(
                "eta_text"
            ),

        "estimated_loops":
            state.get(
                "estimated_loops"
            ),
    }


@app.get(
    "/api/jobs/{job_id}/preview"
)
def preview_v1(
    job_id: str,
):
    path = (
        DATA_DIR
        / job_id
        / "output.mp4"
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Video chưa tồn tại"
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            "Cache-Control":
                "no-store"
        },
    )


@app.get(
    "/api/jobs/{job_id}/download"
)
def download_v1(
    job_id: str,
):
    path = (
        DATA_DIR
        / job_id
        / "output.mp4"
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Video chưa tồn tại"
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=
            f"video-v1-{job_id[:8]}.mp4",
    )


# ==========================================================
# FINAL PREVIEW WORKER (15-30s, same pipeline as final)
# ==========================================================

def run_preview_job(
    job_id: str,
    music_path: Path | str | None,
    music_mode: str,
    music_source: str | None,
    base_volume: float,
    music_volume: float,
    start_seconds: float,
    duration: float,
    config_hash: str,
) -> None:

    job_dir = require_job_dir(job_id)
    paths = preview_paths(job_id)
    base_video = job_dir / "output.mp4"
    tmp_out = paths["tmp"]
    final_out = paths["video"]
    log_path = paths["log"]

    try:
        if isinstance(music_path, str):
            music_path = (
                Path(music_path)
                if music_path
                else None
            )

        video_duration = probe_duration(base_video)

        start, dur = validate_preview_params(
            video_duration,
            start_seconds,
            duration,
        )

        has_music = (
            music_mode != "none"
            and music_path is not None
        )

        if has_music and not music_path.exists():
            raise RuntimeError(
                "BACKGROUND_MUSIC_NOT_FOUND"
            )

        music_duration: float | None = None
        music_offset = 0.0

        if has_music:
            music_duration = probe_duration(
                music_path
            )

            music_offset = music_loop_offset(
                start,
                music_duration,
            )

        # Windowed burn file (same split + offset as final).
        subtitle_ass = build_preview_ass(
            job_id,
            start,
            dur,
        )

        render_logo: RenderLogo | None = (
            get_render_logo(
                job_dir,
                base_video,
            )
        )
        print(f"render_logo={render_logo}")

        render_template: TemplateOverlay | None = (
            resolve_template(
                job_dir,
                base_video,
            )
        )

        write_preview_state(job_id, {
            "status": "processing",
            "progress": 8,
            "message": "Đang render preview...",
            "music_mode": music_mode,
            "music_source":
                music_source if has_music else None,
            "music_path":
                str(music_path) if has_music else None,
            "music_offset": music_offset,
            "base_volume": base_volume,
            "music_volume": music_volume,
            "start_seconds": start,
            "duration": dur,
            "subtitle_ass":
                str(subtitle_ass)
                if subtitle_ass is not None
                else None,
            "logo_enabled": render_logo is not None,
            "template_enabled": render_template is not None,
            "config_hash": config_hash,
            "rendered_seconds": 0.0,
            "started_at": time.time(),
        })

        # Preview always encodes video (frame-accurate seek;
        # final may stream-copy, geometry/filters identical).
        if render_logo is not None:
            command = build_final_with_logo_command(
                base_video=base_video,
                background_music=
                    music_path if has_music else None,
                logo_path=render_logo.path,
                geometry=render_logo.geometry,
                output_path=tmp_out,
                duration=dur,
                base_volume=base_volume,
                music_volume=music_volume,
                subtitle_path=subtitle_ass,
                start_seconds=start,
                music_start_offset=music_offset,
                music_duration=music_duration,
                threads=PREVIEW_THREADS,
                template=render_template,
            )
        else:
            command = build_mix_command(
                base_video=base_video,
                background_music=
                    music_path if has_music else None,
                output_path=tmp_out,
                duration=dur,
                base_volume=base_volume,
                music_volume=music_volume,
                encode_video=True,
                force_encode_video=True,
                subtitle_path=subtitle_ass,
                start_seconds=start,
                music_start_offset=music_offset,
                music_duration=music_duration,
                threads=PREVIEW_THREADS,
                template=render_template,
            )

        result = run_ffmpeg_progress(
            command=command,
            state_path=paths["state"],
            log_path=log_path,
            duration=dur,
            progress_start=5,
            progress_end=98,
            stage_name="Đang render preview",
            pid_file=paths["pid"],
        )

        if result != 0:
            raise RuntimeError(
                ffmpeg_log_tail(log_path)
                or "FFmpeg preview failed"
            )

        if not tmp_out.exists():
            raise RuntimeError(
                "Không tạo được preview"
            )

        tmp_out.replace(final_out)

        state = read_preview_state(job_id) or {}

        state.update({
            "status": "done",
            "progress": 100,
            "message": "Preview sẵn sàng.",
            "rendered_seconds": dur,
            "elapsed_text": format_duration(
                time.time() - state.get("started_at", time.time())
            ),
            "eta_seconds": 0,
        })

        write_preview_state(job_id, state)

    except Exception as exc:

        cancelled = paths["cancel_flag"].exists()

        try:
            current = read_preview_state(job_id) or {}

            # Cancelled by user: keep that status, just cleanup.
            if (
                current.get("status") == "cancelled"
                or cancelled
            ):
                try:
                    current.update({
                        "status": "cancelled",
                        "progress": 0,
                        "message": "Đã hủy preview.",
                    })
                    write_preview_state(job_id, current)
                except Exception:
                    pass
                return
        except Exception:
            pass
        finally:
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass

            try:
                paths["pid"].unlink(missing_ok=True)
            except OSError:
                pass

            try:
                paths["cancel_flag"].unlink(missing_ok=True)
            except OSError:
                pass

        try:
            failed = read_preview_state(job_id) or {}
            failed.update({
                "status": "failed",
                "progress": 0,
                "message": "Preview thất bại.",
                "error": str(exc),
            })
            write_preview_state(job_id, failed)
        except Exception:
            pass

    finally:
        try:
            paths["pid"].unlink(missing_ok=True)
        except OSError:
            pass


@app.post(
    "/api/jobs/{job_id}/mix"
)
def start_mix(
    job_id: str,

    background_music:
        UploadFile | None = File(None),

    music_mode:
        str = Form("default"),

    base_volume:
        float = Form(100),

    music_volume:
        float = Form(15),
):

    job_dir = (
        require_job_dir(job_id)
    )

    base_video = (
        job_dir / "output.mp4"
    )

    if not base_video.exists():
        raise HTTPException(
            404,
            "Không tìm thấy video V1"
        )

    if music_mode not in MUSIC_MODES:
        raise HTTPException(
            400,
            "music_mode phải là "
            "default, upload hoặc none",
        )

    if not (
        0 <= base_volume <= 100
    ):
        raise HTTPException(
            400,
            "Volume video phải từ 0-100"
        )

    if not (
        0 <= music_volume <= 100
    ):
        raise HTTPException(
            400,
            "Volume nhạc phải từ 0-100"
        )

    music_path: Path | None = None
    music_source: str | None = None

    if music_mode == "upload":
        if background_music is None:
            raise HTTPException(
                400,
                "Chế độ upload yêu cầu "
                "file background_music",
            )

        ext = safe_extension(
            background_music.filename,
            AUDIO_EXTENSIONS,
        )

        # Xóa nhạc nền cũ
        for old in job_dir.glob(
            "background_music.*"
        ):
            old.unlink(
                missing_ok=True
            )

        music_path = (
            job_dir
            / f"background_music{ext}"
        )

        save_upload(
            background_music,
            music_path,
            MAX_MUSIC_BYTES,
        )

        if (
            music_path.stat().st_size
            <= 0
        ):
            raise HTTPException(
                400,
                "Nhạc nền rỗng"
            )

        try:
            music_probe = probe_duration(
                music_path
            )
        except Exception:
            music_path.unlink(
                missing_ok=True
            )

            raise HTTPException(
                400,
                "Nhạc nền không đọc được",
            )

        if music_probe <= 0:
            music_path.unlink(
                missing_ok=True
            )

            raise HTTPException(
                400,
                "Nhạc nền không hợp lệ",
            )

        music_source = "upload"

    elif music_mode == "default":
        # System asset: dùng trực tiếp, không copy
        # vào job, không yêu cầu upload.
        if not DEFAULT_BGM_PATH.exists():
            raise HTTPException(
                404,
                "DEFAULT_MUSIC_NOT_FOUND",
            )

        music_path = DEFAULT_BGM_PATH
        music_source = "system"

    else:
        # music_mode == "none": bỏ qua file upload
        # nếu có, không chạy amix.
        music_path = None
        music_source = None

    final_path = (
        job_dir / "final.mp4"
    )

    final_path.unlink(
        missing_ok=True
    )

    write_json(
        job_dir / "mix.json",
        {
            "status": "queued",
            "progress": 5,
            "message":
                "Đã nhận yêu cầu render final.",
            "music_mode":
                music_mode,
            "music_source":
                music_source,
            "base_volume":
                base_volume,
            "music_volume":
                music_volume,
        },
    )

    threading.Thread(
        target=process_mix,
        args=(
            job_id,
            music_path,
            base_volume,
            music_volume,
            music_mode,
            music_source,
        ),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "job_id": job_id,
        "music_mode": music_mode,
    }


@app.get(
    "/api/assets/default-bgm"
)
def get_default_bgm():
    if not DEFAULT_BGM_PATH.exists():
        raise HTTPException(
            404,
            "DEFAULT_MUSIC_NOT_FOUND",
        )

    return FileResponse(
        DEFAULT_BGM_PATH,
        media_type="audio/mpeg",
        filename="default_bgm.mp3",
        headers={
            "Cache-Control":
                "public, max-age=3600",
        },
    )


@app.get(
    "/api/jobs/{job_id}/mix-status"
)
def mix_status(
    job_id: str,
):
    path = (
        DATA_DIR
        / job_id
        / "mix.json"
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Chưa có mix job"
        )

    state = read_json(
        path
    )

    status = state.get(
        "status"
    )

    elapsed = state.get(
        "elapsed_seconds",
        0,
    )

    started = state.get(
        "started_at"
    )

    if (
        status == "processing"
        and started
    ):
        elapsed = max(
            0,
            time.time()
            - float(started),
        )

    return {
        "status":
            status,

        "progress":
            state.get(
                "progress",
                0,
            ),

        "message":
            state.get(
                "message"
            ),

        "error":
            state.get(
                "error"
            ),

        "music_mode":
            state.get(
                "music_mode"
            ),

        "music_source":
            state.get(
                "music_source"
            ),

        "subtitle_enabled":
            state.get(
                "subtitle_enabled"
            ),

        "duration_text":
            state.get(
                "duration_text"
            ),

        "music_duration_text":
            state.get(
                "music_duration_text"
            ),

        "estimated_music_loops":
            state.get(
                "estimated_music_loops"
            ),

        "rendered_time_text":
            state.get(
                "rendered_time_text"
            ),

        "elapsed_text":
            format_duration(
                elapsed
            ),

        "eta_text":
            state.get(
                "eta_text"
            ),

        "base_volume":
            state.get(
                "base_volume"
            ),

        "music_volume":
            state.get(
                "music_volume"
            ),
    }


# ==========================================================
# FINAL PREVIEW 15-30s (same pipeline, window only)
# ==========================================================

@app.post(
    "/api/jobs/{job_id}/final-preview"
)
def start_final_preview(
    job_id: str,

    background_music:
        UploadFile | None = File(None),

    music_mode:
        str = Form("default"),



    base_volume:
        float = Form(100),

    music_volume:
        float = Form(15),

    preview_start_seconds:
        float = Form(0),

    preview_duration:
        float = Form(15),
):
    print(f"FINAL_PREVIEW job_id={job_id}")
    job_dir = require_job_dir(job_id)

    base_video = job_dir / "output.mp4"

    if not base_video.exists():
        raise HTTPException(
            404,
            "Không tìm thấy video V1"
        )

    if music_mode not in MUSIC_MODES:
        raise HTTPException(
            400,
            "music_mode phải là "
            "default, upload hoặc none",
        )

    if not (0 <= base_volume <= 100):
        raise HTTPException(
            400,
            "Volume video phải từ 0-100"
        )

    if not (0 <= music_volume <= 100):
        raise HTTPException(
            400,
            "Volume nhạc phải từ 0-100"
        )

    # Single-flight guard: resume polling the running preview.
    if is_preview_running(job_id):
        return get_preview_status(job_id)

    music_path: Path | None = None
    music_source: str | None = None

    if music_mode == "upload":
        if background_music is not None:
            ext = safe_extension(
                background_music.filename,
                AUDIO_EXTENSIONS,
            )

            for old in job_dir.glob(
                "background_music.*"
            ):
                old.unlink(missing_ok=True)

            music_path = (
                job_dir / f"background_music{ext}"
            )

            save_upload(
                background_music,
                music_path,
                MAX_MUSIC_BYTES,
            )

            if music_path.stat().st_size <= 0:
                raise HTTPException(
                    400,
                    "Nhạc nền rỗng"
                )

            try:
                probe_duration(music_path)
            except Exception:
                music_path.unlink(missing_ok=True)

                raise HTTPException(
                    400,
                    "Nhạc nền không đọc được",
                )

            music_source = "upload"
        else:
            # Reuse previously uploaded music, no re-upload.
            existing = sorted(
                job_dir.glob("background_music.*")
            )

            if not existing:
                raise HTTPException(
                    400,
                    "Chế độ upload yêu cầu "
                    "file background_music",
                )

            music_path = existing[0]
            music_source = "upload"

    elif music_mode == "default":
        if not DEFAULT_BGM_PATH.exists():
            raise HTTPException(
                404,
                "DEFAULT_MUSIC_NOT_FOUND",
            )

        music_path = DEFAULT_BGM_PATH
        music_source = "system"

    else:
        music_path = None
        music_source = None

    try:
        video_duration = probe_duration(base_video)

        start, dur = validate_preview_params(
            video_duration,
            preview_start_seconds,
            preview_duration,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    paths = preview_paths(job_id)

    # Replace previous preview (never reuse its file).
    paths["video"].unlink(missing_ok=True)
    paths["tmp"].unlink(missing_ok=True)
    paths["cancel_flag"].unlink(missing_ok=True)

    config_hash = compute_config_hash(
        job_id=job_id,
        music_mode=music_mode,
        music_path=music_path,
        base_volume=base_volume,
        music_volume=music_volume,
        start_seconds=start,
        duration=dur,
    )

    write_preview_state(job_id, {
        "status": "queued",
        "progress": 2,
        "message": "Đã nhận yêu cầu preview.",
        "music_mode": music_mode,
        "music_source": music_source,
        "music_path":
            str(music_path) if music_path else None,
        "base_volume": base_volume,
        "music_volume": music_volume,
        "start_seconds": start,
        "duration": dur,
        "config_hash": config_hash,
        "started_at": time.time(),
    })

    threading.Thread(
        target=run_preview_job,
        args=(
            job_id,
            str(music_path) if music_path else None,
            music_mode,
            music_source,
            base_volume,
            music_volume,
            start,
            dur,
            config_hash,
        ),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "job_id": job_id,
        "start_seconds": start,
        "duration": dur,
    }


@app.get(
    "/api/jobs/{job_id}/final-preview/status"
)
def final_preview_status(job_id: str):
    state = read_preview_state(job_id)

    if state is None:
        return {"status": "none", "stale": False}

    return get_preview_status(job_id)


@app.get(
    "/api/jobs/{job_id}/final-preview/video"
)
def final_preview_video(job_id: str):
    path = preview_paths(job_id)["video"]

    if not path.exists():
        raise HTTPException(
            404,
            "Preview chưa tồn tại"
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            "Cache-Control":
                "no-store"
        },
    )


@app.post(
    "/api/jobs/{job_id}/final-preview/cancel"
)
def final_preview_cancel(job_id: str):
    return cancel_preview(job_id)


@app.get(
    "/api/jobs/{job_id}/final-preview"
)
def final_preview(
    job_id: str,
):
    path = (
        DATA_DIR
        / job_id
        / "final.mp4"
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Final video chưa tồn tại"
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            "Cache-Control":
                "no-store"
        },
    )


@app.get(
    "/api/jobs/{job_id}/final-download"
)
def final_download(
    job_id: str,
):
    path = (
        DATA_DIR
        / job_id
        / "final.mp4"
    )

    if not path.exists():
        raise HTTPException(
            404,
            "Final video chưa tồn tại"
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=
            f"video-final-{job_id[:8]}.mp4",
    )


# ==========================================================
# SUBTITLE
# ==========================================================

@app.post(
    "/api/jobs/{job_id}/subtitle"
)
def create_subtitle(
    job_id: str,
    model: str = Form("large-v3"),
):
    job_dir = require_job_dir(job_id)

    if not job_dir.exists():
        raise HTTPException(
            404,
            "Job không tồn tại"
        )

    allowed = {"small", "medium", "large-v3", "auto", "gipformer1.5-68M-rnnt", "Zipformer-30M"}

    if model not in allowed:
        raise HTTPException(
            400,
            f"Model không hợp lệ: {model}"
        )

    result = start_subtitle_job(
        job_id=job_id,
        model=model,
    )

    return result


@app.get(
    "/api/jobs/{job_id}/subtitle-status"
)
def subtitle_status(
    job_id: str,
):
    return get_subtitle_status(job_id)




@app.get("/api/colab/drive/status")
def drive_status():
    from services.drive_auth_manager import auth_manager
    return auth_manager.check_status()

@app.post("/api/colab/drive/auth/start")
def drive_auth_start():
    from services.drive_auth_manager import auth_manager
    return auth_manager.start_auth()

@app.post("/api/colab/drive/auth/confirm")
def drive_auth_confirm():
    from services.drive_auth_manager import auth_manager
    return auth_manager.confirm_auth()
    
@app.post("/api/colab/drive/auth/cancel")
def drive_auth_cancel():
    from services.drive_auth_manager import auth_manager
    return auth_manager.cancel_auth()

@app.get("/api/models/status")
def model_status():
    from services.subtitle_service import get_model_status
    return get_model_status()


@app.get("/api/subtitle/runtime-status")
def subtitle_runtime_status():
    from services.drive_auth_manager import auth_manager
    auth = auth_manager.get_status()
    if auth["auth_in_progress"]:
        # Don't run colab commands against the VM while drivemount owns it.
        return {
            **auth,
            "colab_error": None,
            "drive_error": None,
            "ready_for_asr": False,
        }
    status = get_subtitle_runtime_status()
    if status["drive_mounted"] and auth["state"] != "connected":
        auth = auth_manager.check_status()  # e.g. after a restart: re-verify models once
    status["drive_auth_state"] = auth["state"]
    status["drive_models"] = auth["models"]
    if status["drive_mounted"] and auth["state"] == "connected":
        status["drive_message"] = auth["message"]
    if auth["state"] == "failed":
        status["drive_auth_error"] = auth["error"]
    return status


@app.post("/api/models/install")
def install_model(model_name: str = Form(...)):
    from services.subtitle_service import install_model_drive
    return install_model_drive(model_name)

@app.get(
    "/api/jobs/{job_id}/subtitle"
)
def get_subtitle(
    job_id: str,
):
    content = get_subtitle_content(job_id)
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
    )


@app.get(
    "/api/jobs/{job_id}/subtitle/download"
)
def download_subtitle(
    job_id: str,
):
    path = get_subtitle_download_path(job_id)
    return FileResponse(
        path,
        media_type="text/plain",
        filename=f"subtitle-{job_id[:8]}.srt",
    )



@app.post("/api/jobs/{job_id}/subtitle/upload")
async def subtitle_upload(job_id: str, file: UploadFile = File(...)):
    if not file.filename.endswith(".srt"):
        raise HTTPException(400, "Chỉ chấp nhận file .srt")
    require_job_dir(job_id)
    content = await file.read(MAX_SRT_BYTES + 1)
    if len(content) > MAX_SRT_BYTES:
        raise HTTPException(
            413,
            "File vượt quá dung lượng cho phép",
        )
    try:
        text = content.decode("utf-8")
    except:
        text = content.decode("utf-8-sig", errors="replace")
    from services.subtitle_editor_service import setup_subtitle_files
    setup_subtitle_files(job_id, "uploaded", text)
    return {"ok": True, "message": "Đã tải lên phụ đề"}

@app.get("/api/jobs/{job_id}/subtitle/entries")
def get_subtitle_entries_api(job_id: str):
    from services.subtitle_editor_service import get_entries, get_meta, get_layout
    entries = get_entries(job_id)
    meta = get_meta(job_id)
    try:
        layout = get_layout(job_id)
        layout_summary = {
            "subtitle_too_dense": layout.get("subtitle_too_dense", False),
            "dense_source_ids": layout.get("dense_source_ids", []),
            "offset_ms": layout.get("offset_ms", 0),
        }
    except Exception:
        layout_summary = {
            "subtitle_too_dense": False,
            "dense_source_ids": [],
            "offset_ms": 0,
        }
    return {"entries": entries, "meta": meta, "layout": layout_summary}

from pydantic import BaseModel
class SubtitleUpdateEntry(BaseModel):
    text: str
    start_ms: int = None
    end_ms: int = None

@app.patch("/api/jobs/{job_id}/subtitle/entries/{index}")
def update_subtitle_entry_api(job_id: str, index: int, data: SubtitleUpdateEntry):
    from services.subtitle_editor_service import update_entry
    entry = update_entry(job_id, index, data.text, data.start_ms, data.end_ms)
    return {"ok": True, "entry": entry}

@app.post("/api/jobs/{job_id}/subtitle/offset")
def set_subtitle_offset_api(job_id: str, offset_ms: int = Form(...)):
    from services.subtitle_editor_service import set_offset_ms
    offset = set_offset_ms(job_id, offset_ms)
    return {"ok": True, "offset_ms": offset}

@app.post("/api/jobs/{job_id}/subtitle/restore")
def restore_subtitle_api(job_id: str, index: int = Form(None)):
    from services.subtitle_editor_service import restore_entry
    restore_entry(job_id, index)
    return {"ok": True}

@app.get("/api/jobs/{job_id}/subtitle.vtt")
def get_subtitle_vtt(job_id: str, v: str = Query(None)):
    from services.subtitle_editor_service import get_job_dir
    p = get_job_dir(job_id) / "preview.vtt"
    if not p.exists():
        raise HTTPException(404, "No VTT found")
    return FileResponse(str(p), media_type="text/vtt")
    
@app.post("/api/jobs/{job_id}/subtitle/mode")
def set_subtitle_mode_api(job_id: str, mode: str = Form(...)):
    from services.subtitle_editor_service import set_meta
    set_meta(job_id, "subtitle_enabled", mode != "none")
    set_meta(job_id, "subtitle_mode", mode)
    return {"ok": True}


# ==========================================================
# LOGO / WATERMARK
# ==========================================================

class LogoConfigPatch(BaseModel):
    enabled: bool | None = None
    x_norm: float | None = None
    y_norm: float | None = None
    width_norm: float | None = None
    opacity: float | None = None


def _logo_status(job_id: str) -> dict:
    job_dir = require_job_dir(job_id)
    cfg = load_logo_config(job_dir)
    logo_path = find_logo_file(get_logo_dir(job_dir))
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "uploaded": logo_path is not None,
        "file_name": cfg.get("file_name"),
        "x_norm": cfg.get("x_norm"),
        "y_norm": cfg.get("y_norm"),
        "width_norm": cfg.get("width_norm"),
        "opacity": cfg.get("opacity"),
        "aspect_ratio": cfg.get("aspect_ratio"),
    }


@app.post("/api/jobs/{job_id}/logo/upload")
def upload_logo(
    job_id: str,
    logo: UploadFile = File(...),
):
    print(f"LOGO_UPLOAD job_id={job_id}")
    job_dir = require_job_dir(job_id)

    if not (job_dir / "output.mp4").exists():
        raise HTTPException(
            404,
            "Không tìm thấy video V1",
        )

    ext = safe_extension(
        logo.filename,
        set(LOGO_EXTENSIONS),
    )

    logo_dir = get_logo_dir(job_dir)
    logo_dir.mkdir(parents=True, exist_ok=True)

    for old in logo_dir.glob("logo.*"):
        if old.is_file():
            old.unlink(missing_ok=True)

    logo_path = logo_dir / f"logo{ext}"

    save_upload(logo, logo_path, MAX_IMAGE_BYTES)

    if logo_path.stat().st_size <= 0:
        logo_path.unlink(missing_ok=True)
        raise HTTPException(400, "File logo rỗng")

    try:
        width, height = validate_logo_image(logo_path)
    except ValueError as exc:
        logo_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))

    cfg = load_logo_config(job_dir)
    cfg["aspect_ratio"] = round(width / height, 4)
    cfg["file_name"] = logo.filename
    cfg["enabled"] = True
    cfg = clamp_config(cfg)
    save_logo_config(job_dir, cfg)

    return {"ok": True, **_logo_status(job_id)}


@app.patch("/api/jobs/{job_id}/logo/config")
def patch_logo_config(job_id: str, patch: LogoConfigPatch):
    job_dir = require_job_dir(job_id)
    logo_path = find_logo_file(get_logo_dir(job_dir))

    data = patch.model_dump(exclude_none=True)

    if data.get("enabled") and logo_path is None:
        raise HTTPException(400, "LOGO_NOT_FOUND")

    cfg = save_logo_config(job_dir, data)
    return {"ok": True, **_logo_status(job_id)}


@app.get("/api/jobs/{job_id}/logo")
def get_logo(job_id: str):
    return _logo_status(job_id)


@app.get("/api/jobs/{job_id}/logo/file")
def get_logo_file(job_id: str, v: str = Query(None)):
    logo_path = find_logo_file(get_logo_dir(require_job_dir(job_id)))
    if logo_path is None:
        raise HTTPException(404, "LOGO_NOT_FOUND")
    media = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(logo_path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        logo_path,
        media_type=media,
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/jobs/{job_id}/logo")
def delete_logo(job_id: str):
    import shutil as _shutil
    _shutil.rmtree(get_logo_dir(require_job_dir(job_id)), ignore_errors=True)
    return {"ok": True}


# ==========================================================
# TEMPLATE / FRAME OVERLAY
# ==========================================================

class TemplateConfigPatch(BaseModel):
    mode: str | None = None
    system_template: str | None = None
    opacity: float | None = None


def _template_status(job_id: str) -> dict:
    job_dir = require_job_dir(job_id)
    cfg = load_template_config(job_dir)
    template_dir = get_template_dir(job_dir)
    uploaded = None
    for ext in (".svg", ".png", ".webp"):
        candidate = template_dir / f"source{ext}"
        if candidate.is_file():
            uploaded = candidate.name
            break
    return {
        "mode": cfg.get("mode"),
        "enabled": bool(cfg.get("enabled", False)),
        "system_template": cfg.get("system_template"),
        "uploaded_file": uploaded,
        "opacity": cfg.get("opacity"),
    }


@app.get("/api/templates")
def list_templates():
    return {"templates": list_system_templates()}


@app.get("/api/templates/{template_id}/asset")
def get_template_asset(template_id: str):
    path = get_system_template_path(template_id)
    if path is None:
        raise HTTPException(404, "TEMPLATE_NOT_FOUND")
    media = {
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.get("/api/templates/{template_id}/preview")
def preview_template(template_id: str):
    path = get_system_template_path(template_id)
    if path is None:
        raise HTTPException(404, "TEMPLATE_NOT_FOUND")
    if path.suffix.lower() == ".svg":
        from services.template_service import rasterize_svg
        try:
            out = Path("/tmp/loop-video-audio-template-thumb") / f"{template_id}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            rasterize_svg(path.read_bytes(), out, width=480, height=270)
        except Exception:
            raise HTTPException(500, "TEMPLATE_RENDER_FAILED")
        return FileResponse(
            out,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=3600",
            },
        )
    media = {
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.post("/api/jobs/{job_id}/template/upload")
def upload_template(
    job_id: str,
    template: UploadFile = File(...),
):
    job_dir = require_job_dir(job_id)

    if not (job_dir / "output.mp4").exists():
        raise HTTPException(
            404,
            "Không tìm thấy video V1",
        )

    template_dir = get_template_dir(job_dir)
    template_dir.mkdir(parents=True, exist_ok=True)

    # Stage to a temp file first; never use the user filename.
    tmp_path = template_dir / "upload.tmp"

    try:
        save_upload(template, tmp_path, MAX_IMAGE_BYTES)

        try:
            ext = validate_template_upload(
                template.filename,
                tmp_path,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        source = store_template_upload(
            template_dir,
            tmp_path,
            ext,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        cfg = save_template_config(job_dir, {
            "mode": "upload",
            "uploaded_file": source.name,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    _ = cfg
    return {"ok": True, **_template_status(job_id)}


@app.patch("/api/jobs/{job_id}/template/config")
def patch_template_config(job_id: str, patch: TemplateConfigPatch):
    try:
        cfg = save_template_config(
            require_job_dir(job_id),
            patch.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _ = cfg
    return {"ok": True, **_template_status(job_id)}


@app.get("/api/jobs/{job_id}/template")
def get_template(job_id: str):
    return _template_status(job_id)


@app.get("/api/jobs/{job_id}/template/file")
def get_template_file(job_id: str, v: str = Query(None)):
    from services.template_service import find_upload_source
    job_dir = require_job_dir(job_id)
    cfg = load_template_config(job_dir)
    if cfg.get("mode") == "upload":
        source = find_upload_source(get_template_dir(job_dir))
        if source is None:
            raise HTTPException(404, "TEMPLATE_NOT_FOUND")
        if source.suffix.lower() == ".svg":
            # Sanitized raster for DOM preview (never raw user SVG).
            from services.template_service import (
                ensure_raster,
                sanitize_svg,
            )
            try:
                clean = sanitize_svg(source.read_bytes())
                out = Path("/tmp/loop-video-audio-template-thumb") / f"{job_id}.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                from services.template_service import rasterize_svg
                rasterize_svg(clean, out, width=960, height=540)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            except Exception:
                raise HTTPException(500, "TEMPLATE_RENDER_FAILED")
            return FileResponse(
                out,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        media = {
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(source.suffix.lower(), "application/octet-stream")
        return FileResponse(
            source,
            media_type=media,
            headers={"Cache-Control": "no-store"},
        )
    tid = cfg.get("system_template") or "default"
    path = get_system_template_path(tid)
    if path is None:
        raise HTTPException(404, "TEMPLATE_NOT_FOUND")
    if path.suffix.lower() == ".svg":
        return preview_template(tid)
    media = {
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/jobs/{job_id}/template/upload")
def delete_template_upload(job_id: str):
    service_delete_template_upload(require_job_dir(job_id))
    return {"ok": True, **_template_status(job_id)}


@app.post("/api/jobs/{job_id}/template/reset")
def reset_template(job_id: str):
    reset_template_config(require_job_dir(job_id))
    return {"ok": True, **_template_status(job_id)}

@app.delete(
    "/api/jobs/{job_id}/subtitle"
)
def reset_subtitle(
    job_id: str,
):
    reset_subtitle_state(job_id)
    return {"ok": True}


@app.post(
    "/api/jobs/{job_id}/subtitle/chunks/{chunk_index}/retry"
)
def retry_subtitle_chunk(
    job_id: str,
    chunk_index: int,
):
    from services.subtitle_service import retry_chunk
    return retry_chunk(job_id, chunk_index)


@app.get(
    "/api/internal/subtitle-audio/{job_id}"
)
def internal_subtitle_audio(
    job_id: str,
    token: str = Query(...),
):
    from services.subtitle_service import _validate_token

    path = _validate_token(token, job_id)
    
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/subtitle/cancel")
def api_cancel_subtitle(job_id: str):
    from services.subtitle_service import cancel_subtitle_job
    cancel_subtitle_job(job_id)
    return {"ok": True, "message": "Đã hủy Subtitle job."}
