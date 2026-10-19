import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask


# --- Logging -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ytdl")


# --- Constants & Limits --------------------------------------------------------

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

YOUTUBE_URL_PATTERNS = [
    re.compile(r"^https?://(www\.)?youtube\.com/watch\?.*v=[\w-]+"),
    re.compile(r"^https?://youtu\.be/[\w-]+"),
    re.compile(r"^https?://(www\.)?youtube\.com/shorts/[\w-]+"),
    re.compile(r"^https?://(www\.)?youtube\.com/embed/[\w-]+"),
]

DOWNLOAD_TIMEOUT = 300
MAX_URL_LENGTH = 500
MAX_FILESIZE_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))

VALID_VIDEO_QUALITIES = {"best", "1080", "720", "480", "360"}
VALID_AUDIO_QUALITIES = {"best", "320", "192", "128"}

_download_semaphore: asyncio.Semaphore | None = None

# --- Task Store for Live Progress Tracking ------------------------------------

_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()

ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean_ansi(text: str) -> str:
    """Remove ANSI escape codes from yt-dlp formatted strings."""
    return ANSI_REGEX.sub("", text).strip()


def _create_task(task_id: str) -> dict[str, Any]:
    with _tasks_lock:
        task = {
            "task_id": task_id,
            "status": "starting",
            "progress": 0.0,
            "speed": "",
            "eta": "",
            "message": "Connecting...",
            "error": None,
            "filename": None,
            "file_path": None,
            "tmp_dir": None,
            "media_type": None,
            "created_at": time.time(),
        }
        _tasks[task_id] = task
        return task


def _get_task(task_id: str) -> dict[str, Any] | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None


def _update_task(task_id: str, **kwargs) -> None:
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id].update(kwargs)


def _delete_task(task_id: str) -> None:
    with _tasks_lock:
        _tasks.pop(task_id, None)


async def _cleanup_expired_tasks_loop():
    """Periodically clean up task dirs abandoned by users after 10 minutes."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            expired = []
            with _tasks_lock:
                for tid, t in list(_tasks.items()):
                    if now - t.get("created_at", now) > 600:
                        expired.append((tid, t.get("tmp_dir")))
            for tid, tdir in expired:
                if tdir:
                    shutil.rmtree(tdir, ignore_errors=True)
                _delete_task(tid)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Error in task cleanup loop: %s", e)


# --- Lifespan ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise async primitives inside the running event loop."""
    global _download_semaphore
    _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    cleanup_task = asyncio.create_task(_cleanup_expired_tasks_loop())
    logger.info(
        "Startup complete - ffmpeg=%s ffprobe=%s max_concurrent=%d",
        check_ffmpeg(), check_ffprobe(), MAX_CONCURRENT_DOWNLOADS,
    )
    yield
    cleanup_task.cancel()
    logger.info("Shutdown complete.")


# --- App setup -----------------------------------------------------------------

app = FastAPI(
    title="YouTube Downloader API",
    version="2.0.0",
    lifespan=lifespan,
)

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _raw_origins:
    ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://localhost:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
    # FIX #2: Expose Content-Disposition (and others) so browser JavaScript
    # can read the filename from the response headers. Without this, the
    # fetch() call in the frontend gets an empty string for Content-Disposition
    # and falls back to a generic filename like "video.mp4".
    expose_headers=["Content-Disposition", "Content-Length", "Content-Type"],
)


# --- Models --------------------------------------------------------------------

class DownloadRequest(BaseModel):
    url: str
    type: str
    quality: str = "best"


# --- Helper functions ----------------------------------------------------------

def is_valid_youtube_url(url: str) -> bool:
    """Validate a URL against allowed schemes, hosts, and YouTube patterns."""
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            return False
        host = (parsed.netloc or "").lower().split(":")[0]
        if host not in ALLOWED_HOSTS:
            return False
    except Exception:
        return False
    return any(p.match(url.strip()) for p in YOUTUBE_URL_PATTERNS)


def check_ffmpeg() -> bool:
    """Return True if FFmpeg is available on PATH."""
    return shutil.which("ffmpeg") is not None


def check_ffprobe() -> bool:
    """Return True if FFprobe is available on PATH."""
    return shutil.which("ffprobe") is not None


def sanitize_filename(name: str) -> str:
    """Strip characters that are unsafe in filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip(". ")
    return name[:200] or "download"


def make_content_disposition(filename: str) -> str:
    """Build a RFC 5987-compliant Content-Disposition header value."""
    ascii_safe = filename.encode("ascii", errors="ignore").decode("ascii")
    utf8_encoded = urllib.parse.quote(filename, safe="")
    return (
        f'attachment; filename="{ascii_safe}"; '
        f"filename*=UTF-8''{utf8_encoded}"
    )


def _cleanup_dir(directory: str) -> None:
    """Synchronously remove a temporary directory.

    FIX #3: Starlette BackgroundTask expects a plain *synchronous* callable.
    The old code used an async class (_CleanupTask with async __call__), which
    Starlette would schedule but never properly await, silently leaking temp dirs.
    """
    shutil.rmtree(directory, ignore_errors=True)


def _get_cookie_file() -> str | None:
    """Find or materialise a Netscape-format cookies file for yt-dlp."""
    # 1. Local cookies.txt in CWD
    if os.path.exists("cookies.txt"):
        return "cookies.txt"

    # 2. Path from COOKIES_FILE env var
    env_path = os.environ.get("COOKIES_FILE")
    if env_path and os.path.exists(env_path):
        return env_path

    # 3. Raw/base64 cookie content from YOUTUBE_COOKIES env var
    raw_cookies = os.environ.get("YOUTUBE_COOKIES")
    if raw_cookies and raw_cookies.strip():
        content = raw_cookies.strip()
        if not content.startswith("#") and len(content) > 20:
            try:
                import base64
                decoded = base64.b64decode(content).decode("utf-8")
                if "# Netscape" in decoded or "\t" in decoded or ".youtube.com" in decoded:
                    content = decoded
            except Exception:
                pass

        normalized: list[str] = []
        if not content.startswith("#"):
            normalized += [
                "# Netscape HTTP Cookie File",
                "# https://curl.haxx.se/rfc/cookie_spec.html",
                "# This is a generated file! Do not edit.\n",
            ]
        for line in content.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                normalized.append(line_str)
                continue
            if "\t" not in line_str:
                parts = line_str.split(None, 6)
                if len(parts) == 7:
                    line_str = "\t".join(parts)
            normalized.append(line_str)

        cookie_path = Path(tempfile.gettempdir()) / "yt_cookies.txt"
        cookie_path.write_text("\n".join(normalized) + "\n", encoding="utf-8")
        return str(cookie_path)

    return None


def _clean_ytdlp_error(msg: str) -> str:
    """Strip verbose yt-dlp prefixes and tracebacks from error messages."""
    msg = re.sub(r"ERROR:\s*\[[\w\s]+\]\s*[\w-]+:\s*", "", msg)
    first_line = msg.strip().splitlines()[0] if msg.strip() else "Unknown error"
    return first_line[:300]


# --- Routes --------------------------------------------------------------------

@app.get("/health")
def health():
    cookie_file = _get_cookie_file()
    cookies_count = 0
    if cookie_file:
        try:
            import http.cookiejar
            cj = http.cookiejar.MozillaCookieJar()
            cj.load(cookie_file)
            cookies_count = len(cj)
        except Exception:
            cookies_count = -1
    return {
        "status": "ok",
        "ffmpeg": check_ffmpeg(),
        "ffprobe": check_ffprobe(),
        "has_cookies": bool(os.environ.get("YOUTUBE_COOKIES")),
        "cookies_count": cookies_count,
    }


def make_progress_hook(task_id: str):
    def hook(d: dict):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = 0.0
            if total > 0:
                percent = round((downloaded / total) * 100, 1)
            else:
                raw_p = _clean_ansi(str(d.get("_percent_str", ""))).replace("%", "")
                try:
                    percent = round(float(raw_p), 1)
                except ValueError:
                    percent = 0.0

            speed = _clean_ansi(str(d.get("_speed_str", "")))
            eta = _clean_ansi(str(d.get("_eta_str", "")))
            display_percent = min(max(percent, 0.0), 95.0)

            _update_task(
                task_id,
                status="downloading",
                progress=display_percent,
                speed=speed,
                eta=eta,
                downloaded_bytes=downloaded,
                total_bytes=total,
                message=f"Downloading {display_percent:.0f}%",
            )
        elif status == "finished":
            _update_task(
                task_id,
                status="processing",
                progress=96.0,
                speed="",
                eta="",
                message="Processing audio/video streams...",
            )
    return hook


def make_postprocessor_hook(task_id: str):
    def hook(d: dict):
        status = d.get("status")
        pp_name = str(d.get("postprocessor", ""))
        if status == "started":
            msg = "Converting audio to MP3..." if "ExtractAudio" in pp_name else "Processing media..."
            _update_task(
                task_id,
                status="processing",
                progress=97.0,
                speed="",
                eta="",
                message=msg,
            )
        elif status == "finished":
            _update_task(
                task_id,
                status="processing",
                progress=99.0,
                speed="",
                eta="",
                message="Finalizing file...",
            )
    return hook


async def _run_download_task(task_id: str, req: DownloadRequest):
    tmp_dir = tempfile.mkdtemp(prefix="ytdl_")
    _update_task(task_id, tmp_dir=tmp_dir)
    try:
        async with _download_semaphore:
            loop = asyncio.get_running_loop()
            progress_hook = make_progress_hook(task_id)
            postprocessor_hook = make_postprocessor_hook(task_id)

            output_path, filename = await loop.run_in_executor(
                None,
                _do_download_sync,
                req.url.strip(),
                req.type.strip().lower(),
                req.quality.strip().lower() if req.quality else "best",
                tmp_dir,
                progress_hook,
                postprocessor_hook,
                task_id,
            )

        media_type = "audio/mpeg" if req.type.strip().lower() == "audio" else "video/mp4"
        _update_task(
            task_id,
            status="complete",
            progress=100.0,
            message="Download completed ✓",
            filename=filename,
            file_path=output_path,
            tmp_dir=tmp_dir,
            media_type=media_type,
        )
    except HTTPException as exc:
        _update_task(
            task_id,
            status="error",
            error=str(exc.detail),
            message=str(exc.detail),
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("Task %s failed: %s", task_id, exc)
        err_msg = str(exc)
        _update_task(
            task_id,
            status="error",
            error=err_msg,
            message=err_msg,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/download/start")
async def download_start(req: DownloadRequest):
    url = req.url.strip()
    download_type = req.type.strip().lower()

    if not url:
        raise HTTPException(status_code=400, detail="Please enter a YouTube URL.")

    if not is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid YouTube URL (youtube.com or youtu.be).",
        )

    if download_type not in ("video", "audio"):
        raise HTTPException(
            status_code=400,
            detail="Invalid type. Must be 'video' or 'audio'.",
        )

    if not check_ffmpeg():
        raise HTTPException(
            status_code=500,
            detail=(
                "FFmpeg is required but was not found on PATH. "
                "Please install FFmpeg: https://ffmpeg.org/download.html"
            ),
        )

    quality = req.quality.strip().lower() if req.quality else "best"
    if download_type == "video" and quality not in VALID_VIDEO_QUALITIES:
        quality = "best"
    if download_type == "audio" and quality not in VALID_AUDIO_QUALITIES:
        quality = "best"

    task_id = uuid.uuid4().hex
    _create_task(task_id)

    asyncio.create_task(_run_download_task(task_id, req))
    return {"task_id": task_id}


@app.get("/download/progress/{task_id}")
def download_progress(task_id: str):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download session not found or expired.")
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "speed": task.get("speed", ""),
        "eta": task.get("eta", ""),
        "message": task.get("message", ""),
        "filename": task.get("filename"),
        "error": task.get("error"),
    }


@app.get("/download/file/{task_id}")
def download_file(task_id: str):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Download session not found or expired.")

    if task["status"] != "complete":
        if task["status"] == "error":
            raise HTTPException(status_code=400, detail=task.get("error", "Download failed."))
        raise HTTPException(status_code=409, detail="Download is still in progress.")

    file_path = task.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File no longer exists.")

    filename = task.get("filename", "download")
    media_type = task.get("media_type", "application/octet-stream")
    tmp_dir = task.get("tmp_dir")

    def _cleanup_download_task():
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _delete_task(task_id)

    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Content-Disposition": make_content_disposition(filename)},
        background=BackgroundTask(_cleanup_download_task),
    )


@app.post("/download")
async def download(req: DownloadRequest):

    # 1. Validate inputs -------------------------------------------------------

    url = req.url.strip()
    download_type = req.type.strip().lower()

    if not url:
        raise HTTPException(status_code=400, detail="Please enter a YouTube URL.")

    if not is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid YouTube URL (youtube.com or youtu.be).",
        )

    if download_type not in ("video", "audio"):
        raise HTTPException(
            status_code=400,
            detail="Invalid type. Must be 'video' or 'audio'.",
        )

    quality = req.quality.strip().lower() if req.quality else "best"
    if download_type == "video" and quality not in VALID_VIDEO_QUALITIES:
        quality = "best"
    if download_type == "audio" and quality not in VALID_AUDIO_QUALITIES:
        quality = "best"

    # 2. Check FFmpeg ----------------------------------------------------------

    if not check_ffmpeg():
        raise HTTPException(
            status_code=500,
            detail=(
                "FFmpeg is required but was not found on PATH. "
                "Please install FFmpeg: https://ffmpeg.org/download.html"
            ),
        )

    # 3. Download (offload blocking I/O to thread pool) -----------------------

    tmp_dir = tempfile.mkdtemp(prefix="ytdl_")

    try:
        # FIX #4: yt-dlp's extract_info() is blocking (network + disk I/O).
        # Running it directly in an async function freezes the entire event
        # loop, making the server unable to handle any other requests during
        # the download. We offload it to the default thread-pool executor.
        async with _download_semaphore:
            loop = asyncio.get_running_loop()
            output_path, filename = await loop.run_in_executor(
                None,
                _do_download_sync,
                url,
                download_type,
                quality,
                tmp_dir,
            )

        media_type = "audio/mpeg" if download_type == "audio" else "video/mp4"
        headers = {"Content-Disposition": make_content_disposition(filename)}

        return FileResponse(
            path=output_path,
            media_type=media_type,
            headers=headers,
            background=BackgroundTask(_cleanup_dir, tmp_dir),
        )

    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("Unexpected error for %s", url)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# --- Core download logic (synchronous - runs in thread pool) ------------------

def _do_download_sync(
    url: str,
    download_type: str,
    quality: str,
    tmp_dir: str,
    progress_hook: Any = None,
    postprocessor_hook: Any = None,
    task_id: str | None = None,
) -> tuple[str, str]:
    """
    Download YouTube media via yt-dlp.

    Intentionally *synchronous* so it can be safely offloaded to a
    ThreadPoolExecutor via asyncio.loop.run_in_executor(), preventing yt-dlp's
    blocking network I/O from stalling the asyncio event loop.

    Returns (output_file_path, display_filename).
    """

    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE_BYTES,
        "outtmpl": outtmpl,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
            }
        },
    }
    if progress_hook:
        base_opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        base_opts["postprocessor_hooks"] = [postprocessor_hook]

    if download_type == "audio":
        bitrate = quality if quality != "best" else "0"
        ydl_opts = {
            **base_opts,
            "format": "bestaudio/b/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }
            ],
        }
    else:
        if quality == "best":
            fmt = "bv*+ba/b/best"
        else:
            height = int(quality)
            fmt = (
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]/"
                f"bv*+ba/"
                f"b/"
                f"best"
            )
        ydl_opts = {
            **base_opts,
            "format": fmt,
            "format_sort": ["res", "vcodec:h264", "acodec:m4a", "ext:mp4"],
            "merge_output_format": "mp4",
        }

    cookie_path = _get_cookie_file()
    info: dict | None = None

    # Run yt-dlp (with optional cookie fallback) -------------------------------

    try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

        except yt_dlp.utils.DownloadError as first_err:
            first_msg = str(first_err)
            logger.warning("Clean attempt failed: %s", _clean_ytdlp_error(first_msg))
            if cookie_path:
                cookie_opts = {
                    **ydl_opts,
                    "cookiefile": cookie_path,
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["web", "mweb", "android", "ios"],
                        }
                    },
                }
                try:
                    with yt_dlp.YoutubeDL(cookie_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                except yt_dlp.utils.DownloadError as second_err:
                    logger.warning("Cookie attempt failed: %s", _clean_ytdlp_error(str(second_err)))
                    raise second_err
            else:
                raise first_err

    except HTTPException:
        raise

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        logger.error("yt-dlp DownloadError: %s", _clean_ytdlp_error(msg))

        if (
            "Sign in to confirm" in msg
            or "bot" in msg.lower()
            or "failed to extract any player response" in msg.lower()
        ):
            raise HTTPException(
                status_code=403,
                detail="YouTube requested bot verification for this server IP. Providing updated cookies or retrying resolves this.",
            )
        if "Video unavailable" in msg or "not available" in msg.lower():
            raise HTTPException(status_code=400, detail="This video is unavailable.")
        if "Private video" in msg or "private" in msg.lower():
            raise HTTPException(status_code=400, detail="This video is private.")
        if "members-only" in msg.lower():
            raise HTTPException(status_code=400, detail="This video is members-only.")
        if "Unsupported URL" in msg or "is not a valid URL" in msg:
            raise HTTPException(
                status_code=400,
                detail="Unsupported URL. Please use a valid YouTube link.",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Download failed: {_clean_ytdlp_error(msg)}",
        ) from exc

    except Exception as exc:
        logger.exception("Unexpected error during yt-dlp extraction")
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error: {exc}",
        ) from exc

    # Locate the downloaded file -----------------------------------------------

    tmp_path = Path(tmp_dir)
    files = [f for f in tmp_path.iterdir() if f.is_file()]

    if not files:
        raise HTTPException(
            status_code=500,
            detail="Download produced no output file.",
        )

    title = sanitize_filename(info.get("title", "download") if info else "download")

    # Audio: pick the largest file (the MP3) -----------------------------------

    if download_type == "audio":
        output_file = max(files, key=lambda f: f.stat().st_size)
        return str(output_file), f"{title}.mp3"

    # Video: find the video file, remux if needed ------------------------------

    video_extensions = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
    video_files = [f for f in files if f.suffix.lower() in video_extensions]

    if not video_files:
        raise HTTPException(
            status_code=500,
            detail="No video file was produced by yt-dlp.",
        )

    input_file = max(video_files, key=lambda f: f.stat().st_size)
    filename = f"{title}.mp4"

    # Already an MP4 - serve directly, no re-encoding needed
    if input_file.suffix.lower() == ".mp4":
        return str(input_file), filename

    # Non-MP4 container: remux to MP4 (3 escalating attempts)
    if task_id:
        _update_task(
            task_id,
            status="processing",
            progress=98.0,
            message="Optimizing video format...",
        )
    output_file = tmp_path / f"{title}_out.mp4"

    def _run_ffmpeg(cmd: list, timeout: int) -> bool:
        """Run an ffmpeg command, return True on success."""
        try:
            r = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return (
                r.returncode == 0
                and output_file.exists()
                and output_file.stat().st_size > 0
            )
        except Exception:
            return False

    # Attempt 1: instant stream copy (~1 s, near-zero CPU)
    if _run_ffmpeg(
        ["ffmpeg", "-y", "-i", str(input_file), "-c", "copy",
         "-movflags", "+faststart", str(output_file)],
        timeout=30,
    ):
        input_file.unlink(missing_ok=True)
        return str(output_file), filename

    # Attempt 2: copy video stream, transcode audio to AAC (~2-3 s)
    if _run_ffmpeg(
        ["ffmpeg", "-y", "-i", str(input_file),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(output_file)],
        timeout=60,
    ):
        input_file.unlink(missing_ok=True)
        return str(output_file), filename

    # Attempt 3: full H.264 + AAC re-encode (ultrafast preset)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(input_file),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=500,
            detail="Video re-encoding timed out.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Video conversion failed: {exc}",
        ) from exc

    if not output_file.exists() or output_file.stat().st_size == 0:
        return str(input_file), f"{title}{input_file.suffix}"

    input_file.unlink(missing_ok=True)
    return str(output_file), filename


# --- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
