from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from services.subtitle_service import (
    get_subtitle_content,
    get_subtitle_status,
    reset_subtitle_state,
    start_subtitle_job,
)


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

        <video
            id="baseVideo"
            controls
            playsinline
            preload="metadata"
        ></video>

        <div
            id="baseVideoMeta"
            class="video-meta"
        ></div>

        <div class="field">

            <label>
                Nhạc nền
            </label>

            <input
                id="backgroundMusic"
                type="file"
            >

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


        <div class="volume-card">

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



                <div id="driveAuthPanel" class="box" style="margin-top:12px;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <strong>Google Drive</strong>
                        <span id="driveStatusBadge" class="badge bg-secondary">Checking...</span>
                    </div>
                    <small id="driveStatusText" style="display:block; margin-top:4px;">Session mới cần cấp lại quyền Drive.</small>
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


        <div class="actions">

            <a
                id="downloadV1"
                class="action secondary"
            >
                TẢI BẢN V1
            </a>

            <button
                id="mixButton"
                class="primary"
                disabled
            >
                RENDER + NHẠC NỀN
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
            Chọn nhạc nền để tiếp tục.
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


backgroundMusic.addEventListener(
    "change",
    () => {
        const file =
            backgroundMusic.files[0];

        if (!file) {
            mixButton.disabled = true;
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

        mixButton.disabled = false;

        mixStatus.textContent =
            `Đã chọn nhạc nền: ${file.name}`;
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

    downloadV1.href =
        `/api/jobs/${jobId}/download`;

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
                if (data && data.detail && data.detail.code === "DRIVE_AUTH_REQUIRED") {
                    throw new Error("Google Drive cần kết nối lại cho Colab session mới. Vui lòng bấm Kết nối Google Drive ở trên.");
                }
                throw new Error(
                    (data && typeof data.detail === 'string' ? data.detail : false) ||
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

            showSubtitleError(
                subtitleStatus,
                data.error ||
                "Tạo SRT thất bại"
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

        const music =
            backgroundMusic.files[0];

        if (!music) {
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
            "Đang upload nhạc nền...";

        mixRuntimeDetail.textContent =
            "Runtime 00:00:00";

        mixStatus.textContent =
            "Đang chuẩn bị render final...";

        const form =
            new FormData();

        form.append(
            "background_music",
            music
        );

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

async function checkModelStatus() {
    try {
        const response = await fetch('/api/models/status');
        const data = await response.json();
        
        const badge = document.getElementById('gipformerStatusBadge');
        const text = document.getElementById('gipformerStatusText');
        const btn = document.getElementById('btnInstallGipformer');
        
        if (data.gipformer) {
            if (data.gipformer.drive_auth_required) {
                badge.className = 'badge bg-danger';
                badge.textContent = 'Drive Auth Required';
                text.textContent = 'Vui lòng mở Colab và mount Google Drive.';
                btn.style.display = 'none';
            } else if (data.gipformer.model_runtime_ready) {
                badge.className = 'badge bg-success';
                badge.textContent = 'Ready';
                text.textContent = 'Google Drive: Connected | Gipformer: Ready';
                btn.style.display = 'none';
            } else if (data.gipformer.model_on_drive) {
                badge.className = 'badge bg-success';
                badge.textContent = 'Cached on Drive';
                text.textContent = 'Google Drive: Connected | Gipformer: Cached';
                btn.style.display = 'none';
            } else {
                badge.className = 'badge bg-warning';
                badge.textContent = 'Not installed';
                text.textContent = 'Google Drive: Connected | Gipformer: Not installed';
                btn.style.display = 'inline-block';
            }
        }
    } catch (e) {
        console.error("Failed to check model status", e);
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
            const statusRes = await fetch('/api/models/status');
            const statusData = await statusRes.json();
            if (statusData.gipformer && (statusData.gipformer.model_on_drive || statusData.gipformer.model_runtime_ready)) {
                clearInterval(interval);
                checkModelStatus();
            } else {
                const badge = document.getElementById('gipformerStatusBadge');
                badge.textContent = statusData.gipformer?.install_status || 'Installing...';
            }
        }, 3000);
    } catch(e) {
        alert(e.message);
        btn.disabled = false;
        btn.textContent = 'Tải model vào Drive';
    }
});

// Initial check
checkModelStatus();


let driveAuthInterval = null;

async function pollDriveAuth() {
    try {
        const res = await fetch("/api/colab/drive/status");
        if (!res.ok) return;
        const data = await res.json();
        
        const badge = document.getElementById("driveStatusBadge");
        const text = document.getElementById("driveStatusText");
        const btnConnect = document.getElementById("btnConnectDrive");
        const linkContainer = document.getElementById("driveAuthLinkContainer");
        const authLink = document.getElementById("driveAuthLink");
        
        if (data.drive_mounted) {
            badge.textContent = "Connected";
            badge.className = "badge bg-success";
            text.textContent = data.message || "Đã kết nối";
            btnConnect.style.display = "none";
            linkContainer.style.display = "none";
        } else if (data.auth_in_progress) {
            badge.textContent = "Waiting";
            badge.className = "badge bg-warning text-dark";
            text.textContent = data.message || "Đang chờ...";
            btnConnect.style.display = "none";
            if (data.oauth_url) {
                linkContainer.style.display = "block";
                authLink.href = data.oauth_url;
            } else {
                linkContainer.style.display = "none";
            }
        } else {
            badge.textContent = "Disconnected";
            badge.className = "badge bg-danger";
            text.textContent = data.message || "Cần kết nối Drive";
            btnConnect.style.display = "block";
            linkContainer.style.display = "none";
        }
    } catch(e) {
        console.error(e);
    }
}

document.getElementById("btnConnectDrive")?.addEventListener("click", async () => {
    document.getElementById("btnConnectDrive").disabled = true;
    try {
        await fetch("/api/colab/drive/auth/start", {method: "POST"});
        pollDriveAuth();
    } catch(e) {}
    document.getElementById("btnConnectDrive").disabled = false;
});

document.getElementById("btnConfirmAuth")?.addEventListener("click", async () => {
    document.getElementById("btnConfirmAuth").disabled = true;
    try {
        await fetch("/api/colab/drive/auth/confirm", {method: "POST"});
        pollDriveAuth();
    } catch(e) {}
    document.getElementById("btnConfirmAuth").disabled = false;
});

document.getElementById("btnCancelAuth")?.addEventListener("click", async () => {
    try {
        await fetch("/api/colab/drive/auth/cancel", {method: "POST"});
        pollDriveAuth();
    } catch(e) {}
});


let subEntries = [];
let subMeta = {};
let activeCueIndex = -1;
let subTrack = null;

async function loadSubtitleEditor() {
    if (!currentJobId) return;
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/entries`);
        if (!res.ok) throw new Error("Could not load subtitles");
        const data = await res.json();
        subEntries = data.entries;
        subMeta = data.meta;
        renderSubtitleEditor();
        updateSubtitleInfo();
        reloadVideoTrack();
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
                        activeCueIndex = parseInt(cue.id) - 1;
                        highlightCue(activeCueIndex);
                        renderCustomOverlay(cue.text);
                    } else {
                        activeCueIndex = -1;
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
        overlay = document.createElement("div");
        overlay.id = "customSubOverlay";
        overlay.style.position = "absolute";
        overlay.style.bottom = "10%";
        overlay.style.left = "0";
        overlay.style.width = "100%";
        overlay.style.textAlign = "center";
        overlay.style.color = "white";
        overlay.style.textShadow = "2px 2px 4px #000, -2px -2px 4px #000, 2px -2px 4px #000, -2px 2px 4px #000";
        overlay.style.fontSize = "24px";
        overlay.style.fontWeight = "bold";
        overlay.style.pointerEvents = "auto";
        overlay.style.cursor = "pointer";
        overlay.style.zIndex = "10";
        overlay.style.padding = "0 20px";
        overlay.style.boxSizing = "border-box";
        v.parentNode.style.position = "relative";
        v.parentNode.appendChild(overlay);
        
        overlay.addEventListener("click", () => {
            if (activeCueIndex >= 0) {
                const v = document.getElementById("baseVideo");
                v.pause();
                openSubtitleEditor(activeCueIndex);
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
    subEntries.forEach((entry, i) => {
        if (filterText && !entry.text.toLowerCase().includes(filterText)) return;
        
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
            const data = await res.json();
            subEntries[idx] = data.entry;
            subMeta.edited_count = (subMeta.edited_count || 0) + 1;
            renderSubtitleEditor();
            updateSubtitleInfo();
            reloadVideoTrack(); // update VTT
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

function openSubtitleEditor(focusIdx = -1) {
    document.getElementById("subtitleEditorModal").style.display = "flex";
    renderSubtitleEditor();
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

document.getElementById("btnRestoreAll")?.addEventListener("click", async () => {
    if (confirm("Tất cả chỉnh sửa thủ công sẽ bị mất. Bạn có chắc muốn khôi phục?")) {
        try {
            const form = new FormData();
            const res = await fetch(`/api/jobs/${currentJobId}/subtitle/restore`, {method: "POST", body: form});
            if (res.ok) await loadSubtitleEditor();
        } catch(e) {}
    }
});

document.getElementById("btnToggleSubtitle")?.addEventListener("click", async () => {
    try {
        const form = new FormData();
        form.append("mode", subMeta.subtitle_enabled ? "none" : (subMeta.subtitle_mode || "auto"));
        const res = await fetch(`/api/jobs/${currentJobId}/subtitle/mode`, {method: "POST", body: form});
        if (res.ok) await loadSubtitleEditor();
    } catch(e) {}
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

// Start polling
setInterval(pollDriveAuth, 3000);
pollDriveAuth();

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
) -> None:
    with destination.open(
        "wb"
    ) as output:
        shutil.copyfileobj(
            upload.file,
            output,
            length=1024 * 1024,
        )


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
        DATA_DIR / job_id
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

def build_mix_command(
    *,
    base_video: Path,
    background_music: Path | None,
    output_path: Path,
    duration: float,
    base_volume: float,
    music_volume: float,
    encode_video: bool,
) -> list[str]:

    base_gain = (
        base_volume / 100.0
    )

    music_gain = (
        music_volume / 100.0
    )

    filter_complex = (
        f"[0:a:0]"
        f"volume={base_gain:.4f}"
        f"[voice];"

        f"[1:a:0]"
        f"volume={music_gain:.4f}"
        f"[music];"

        f"[voice][music]"
        f"amix="
        f"inputs=2:"
        f"duration=first:"
        f"dropout_transition=0:"
        f"normalize=0,"

        f"alimiter="
        f"limit=0.95"
        f"[mixed]"
    )

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(base_video),

        # Loop nhạc nền vô hạn
        "-stream_loop",
        "-1",

        "-i",
        str(background_music),

        "-filter_complex",
        filter_complex,

        "-map",
        "0:v:0",

        "-map",
        "[mixed]",

        "-t",
        f"{duration:.6f}",
    ]

    if encode_video:
        command += [
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "20",

            "-pix_fmt",
            "yuv420p",
        ]
    else:
        command += [
            "-c:v",
            "copy",
        ]

    command += [
        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-ar",
        "48000",

        "-movflags",
        "+faststart",

        "-progress",
        "pipe:1",

        "-nostats",

        str(output_path),
    ]

    return command


def process_mix(
    job_id: str,
    background_music: Path | None,
    base_volume: float,
    music_volume: float,
) -> None:

    job_dir = (
        DATA_DIR / job_id
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

        music_duration = (
            probe_duration(
                background_music
            )
        )

        loops = max(
            1,
            int(
                duration
                / music_duration
            ) + 1,
        )

        state = {
            "status":
                "processing",

            "progress":
                10,

            "message":
                "Đang chuẩn bị nhạc nền...",

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
                ),

            "estimated_music_loops":
                loops,

            "base_volume":
                base_volume,

            "music_volume":
                music_volume,

            "rendered_time_text":
                "00:00:00",

            "started_at":
                started,
        }

        write_json(
            mix_state_path,
            state,
        )

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

                encode_video=False,
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
                    "Đang mix nhạc nền",
            )
        )

        if result != 0:

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
        DATA_DIR / job_id
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
        )

        save_upload(
            audio,
            audio_path,
        )

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
        DATA_DIR / job_id
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


@app.post(
    "/api/jobs/{job_id}/mix"
)
def start_mix(
    job_id: str,

    background_music:
        UploadFile = File(...),

    base_volume:
        float = Form(100),

    music_volume:
        float = Form(15),
):

    job_dir = (
        DATA_DIR / job_id
    )

    base_video = (
        job_dir / "output.mp4"
    )

    if not base_video.exists():
        raise HTTPException(
            404,
            "Không tìm thấy video V1"
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
    )

    if (
        music_path.stat().st_size
        <= 0
    ):
        raise HTTPException(
            400,
            "Nhạc nền rỗng"
        )

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
                "Đã nhận nhạc nền.",
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
        ),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "job_id": job_id,
    }


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
    job_dir = DATA_DIR / job_id

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
    auth_manager.check_status()
    return auth_manager.get_status()

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
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except:
        text = content.decode("utf-8-sig", errors="replace")
    from services.subtitle_editor_service import setup_subtitle_files
    setup_subtitle_files(job_id, "uploaded", text)
    return {"ok": True, "message": "Đã tải lên phụ đề"}

@app.get("/api/jobs/{job_id}/subtitle/entries")
def get_subtitle_entries_api(job_id: str):
    from services.subtitle_editor_service import get_entries, get_meta
    entries = get_entries(job_id)
    meta = get_meta(job_id)
    return {"entries": entries, "meta": meta}

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
