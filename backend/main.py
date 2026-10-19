import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

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

# FIX #1: asyncio.Semaphore must be created inside the running event loop,
# NOT at module level. Module-level creation causes DeprecationWarning in
# Python 3.10 and RuntimeError in Python 3.12+.
_download_semaphore: asyncio.Semaphore | None = None


# --- Lifespan ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise async primitives inside the running event loop."""
    global _download_semaphore
    _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    logger.info(
        "Startup complete - ffmpeg=%s ffprobe=%s max_concurrent=%d",
        check_ffmpeg(), check_ffprobe(), MAX_CONCURRENT_DOWNLOADS,
    )
    yield
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
) -> tuple[str, str]:
    """
    Download YouTube media via yt-dlp.

    Intentionally *synchronous* so it can be safely offloaded to a
    ThreadPoolExecutor via asyncio.loop.run_in_executor(), preventing yt-dlp's
    blocking network I/O from stalling the asyncio event loop.

    Returns (output_file_path, display_filename).
    """

    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    # FIX #5: Removed invalid 'js_runtimes' and 'remote_components' keys that
    # are not recognised by yt-dlp and caused silent errors / warnings.
    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE_BYTES,
        "outtmpl": outtmpl,
    }

    if download_type == "audio":
        bitrate = quality if quality != "best" else "0"
        ydl_opts = {
            **base_opts,
            "format": "bestaudio/best",
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
            fmt = "bv*+ba/best"
        else:
            height = int(quality)
            fmt = (
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]/"
                f"bv*+ba/best"
            )
        ydl_opts = {
            **base_opts,
            "format": fmt,
            "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4"],
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
            logger.warning("First attempt failed: %s", _clean_ytdlp_error(first_msg))
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                except yt_dlp.utils.DownloadError as second_err:
                    second_msg = str(second_err)
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"Download failed even with cookies. "
                            f"Clean: {_clean_ytdlp_error(first_msg)} | "
                            f"Cookies: {_clean_ytdlp_error(second_msg)}"
                        ),
                    )
            else:
                raise first_err

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        logger.error("yt-dlp DownloadError: %s", _clean_ytdlp_error(msg))

        if "Sign in to confirm" in msg or "bot" in msg.lower():
            raise HTTPException(
                status_code=403,
                detail="YouTube requested bot verification. Try again later or provide cookies.",
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
            detail=f"Unexpected error ({type(exc).__name__}): {exc}",
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
