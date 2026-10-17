import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="YouTube Downloader API",
    version="1.0.0",
)

# Parse explicit allowed origins from environment (comma-separated), falling back to local dev
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _raw_origins:
    ALLOWED_ORIGINS = [orig.strip() for orig in _raw_origins.split(",") if orig.strip()]
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
)


# ─── Constants & Limits ────────────────────────────────────────────────────────

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

YOUTUBE_URL_PATTERNS = [
    re.compile(
        r"^https?://(www\.)?youtube\.com/watch\?.*v=[\w-]+"
    ),
    re.compile(
        r"^https?://youtu\.be/[\w-]+"
    ),
    re.compile(
        r"^https?://(www\.)?youtube\.com/shorts/[\w-]+"
    ),
    re.compile(
        r"^https?://(www\.)?youtube\.com/embed/[\w-]+"
    ),
]

DOWNLOAD_TIMEOUT = 300
MAX_URL_LENGTH = 200
MAX_FILESIZE_BYTES = 500 * 1024 * 1024  # 500 MB max to protect server resources
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ─── Models ───────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str
    type: str
    quality: str = "best"


VALID_VIDEO_QUALITIES = {
    "best",
    "1080",
    "720",
    "480",
    "360",
}

VALID_AUDIO_QUALITIES = {
    "best",
    "320",
    "192",
    "128",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_valid_youtube_url(url: str) -> bool:
    """Validate submitted URL against allowed schemes, hosts, and YouTube patterns."""

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

    return any(
        pattern.match(url.strip())
        for pattern in YOUTUBE_URL_PATTERNS
    )


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
    """Build a RFC 5987-compliant Content-Disposition header."""

    ascii_safe = (
        filename
        .encode("ascii", errors="ignore")
        .decode("ascii")
    )

    utf8_encoded = urllib.parse.quote(
        filename,
        safe="",
    )

    return (
        f'attachment; filename="{ascii_safe}"; '
        f"filename*=UTF-8''{utf8_encoded}"
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "ffmpeg": check_ffmpeg(),
        "ffprobe": check_ffprobe(),
    }


@app.post("/download")
async def download(req: DownloadRequest):

    # 1. Validate input

    url = req.url.strip()
    download_type = req.type.strip().lower()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Please enter a YouTube URL.",
        )

    if not is_valid_youtube_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Please enter a valid YouTube URL "
                "(youtube.com or youtu.be)."
            ),
        )

    if download_type not in ("video", "audio"):
        raise HTTPException(
            status_code=400,
            detail="Invalid type. Must be 'video' or 'audio'.",
        )

    quality = (
        req.quality.strip().lower()
        if req.quality
        else "best"
    )

    if download_type == "video":
        if quality not in VALID_VIDEO_QUALITIES:
            quality = "best"

    if download_type == "audio":
        if quality not in VALID_AUDIO_QUALITIES:
            quality = "best"

    # 2. Check FFmpeg

    if not check_ffmpeg():
        raise HTTPException(
            status_code=500,
            detail=(
                "FFmpeg is required for this operation but was not found. "
                "Please install FFmpeg and make sure it is on your PATH."
            ),
        )

    # 3. Create temporary directory

    tmp_dir = tempfile.mkdtemp(
        prefix="ytdl_"
    )

    try:
        async with DOWNLOAD_SEMAPHORE:
            output_path, filename = await _do_download(
                url,
                download_type,
                quality,
                tmp_dir,
            )

        media_type = (
            "audio/mpeg"
            if download_type == "audio"
            else "video/mp4"
        )

        headers = {
            "Content-Disposition": make_content_disposition(
                filename
            )
        }

        return FileResponse(
            path=output_path,
            media_type=media_type,
            headers=headers,
            background=_CleanupTask(tmp_dir),
        )

    except HTTPException:

        shutil.rmtree(
            tmp_dir,
            ignore_errors=True,
        )

        raise

    except Exception as exc:

        shutil.rmtree(
            tmp_dir,
            ignore_errors=True,
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


# ─── Cookie & Client Helper ───────────────────────────────────────────────────

def _get_cookie_file() -> str | None:
    """Find or materialize cookies for yt-dlp to bypass bot verification."""
    # 1. Local cookies.txt file
    if os.path.exists("cookies.txt"):
        return "cookies.txt"

    # 2. Path provided via COOKIES_FILE env var
    env_path = os.environ.get("COOKIES_FILE")
    if env_path and os.path.exists(env_path):
        return env_path

    # 3. Cookies content provided via YOUTUBE_COOKIES env var (raw text or base64)
    raw_cookies = os.environ.get("YOUTUBE_COOKIES")
    if raw_cookies and raw_cookies.strip():
        content = raw_cookies.strip()
        # Attempt base64 decoding if not starting with Netscape header or standard comments
        if not content.startswith("#") and len(content) > 20:
            try:
                import base64
                decoded = base64.b64decode(content).decode("utf-8")
                if "# Netscape" in decoded or "\t" in decoded:
                    content = decoded
            except Exception:
                pass
        cookie_path = Path(tempfile.gettempdir()) / "yt_cookies.txt"
        cookie_path.write_text(content, encoding="utf-8")
        return str(cookie_path)

    return None


# ─── Download logic ───────────────────────────────────────────────────────────

async def _do_download(
    url: str,
    download_type: str,
    quality: str,
    tmp_dir: str,
):
    """Download media with yt-dlp."""

    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s",
    )

    # ──────────────────────────────────────────────────────────────────────────
    # AUDIO DOWNLOAD
    # ──────────────────────────────────────────────────────────────────────────

    if download_type == "audio":

        bitrate = (
            quality
            if quality != "best"
            else "0"
        )

        ydl_opts = {
            "format": "bestaudio/best",

            "outtmpl": outtmpl,

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }
            ],

            "js_runtimes": {
                "node": {}
            },

            "quiet": True,
            "no_warnings": True,

            "socket_timeout": DOWNLOAD_TIMEOUT,

            "retries": 3,

            "noplaylist": True,
            "max_filesize": MAX_FILESIZE_BYTES,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # VIDEO DOWNLOAD
    # ──────────────────────────────────────────────────────────────────────────

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
            "format": fmt,

            "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4"],

            "merge_output_format": "mp4",

            "outtmpl": outtmpl,

            "js_runtimes": {
                "node": {}
            },

            "quiet": True,
            "no_warnings": True,

            "socket_timeout": DOWNLOAD_TIMEOUT,

            "retries": 3,

            "noplaylist": True,
            "max_filesize": MAX_FILESIZE_BYTES,
        }

    # Apply cookies or fallback player clients
    cookie_path = _get_cookie_file()
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path
    else:
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        }

    # ──────────────────────────────────────────────────────────────────────────
    # RUN YT-DLP
    # ──────────────────────────────────────────────────────────────────────────

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

    except yt_dlp.utils.DownloadError as exc:

        msg = str(exc)

        if "Sign in to confirm you’re not a bot" in msg or "bot" in msg.lower():
            raise HTTPException(
                status_code=403,
                detail=(
                    "YouTube requested bot verification for this server IP. "
                    "Please set the YOUTUBE_COOKIES environment variable in Render with your exported cookies.txt."
                ),
            )

        if (
            "Video unavailable" in msg
            or "This video is not available" in msg
        ):
            raise HTTPException(
                status_code=400,
                detail="This video is unavailable.",
            )

        if (
            "Private video" in msg
            or "private" in msg.lower()
        ):
            raise HTTPException(
                status_code=400,
                detail="This video is private.",
            )

        if "members-only" in msg.lower():

            raise HTTPException(
                status_code=400,
                detail="This video is members-only.",
            )

        if (
            "Unsupported URL" in msg
            or "is not a valid URL" in msg
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unsupported URL. "
                    "Please use a valid YouTube link."
                ),
            )

        raise HTTPException(
            status_code=500,
            detail=(
                f"Download failed: "
                f"{_clean_ytdlp_error(msg)}"
            ),
        ) from exc

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                "An unexpected error occurred "
                "during download."
            ),
        ) from exc

    # ──────────────────────────────────────────────────────────────────────────
    # FIND DOWNLOADED FILE
    # ──────────────────────────────────────────────────────────────────────────

    tmp_path = Path(tmp_dir)

    files = [
        file
        for file in tmp_path.iterdir()
        if file.is_file()
    ]

    if not files:

        raise HTTPException(
            status_code=500,
            detail="Download produced no output file.",
        )

    # Get title from YouTube information

    title = sanitize_filename(
        info.get(
            "title",
            "download",
        )
    )

    # ──────────────────────────────────────────────────────────────────────────
    # AUDIO
    # ──────────────────────────────────────────────────────────────────────────

    if download_type == "audio":

        output_file = max(
            files,
            key=lambda file: file.stat().st_size,
        )

        filename = f"{title}.mp3"

        return str(output_file), filename

    # ──────────────────────────────────────────────────────────────────────────
    # VIDEO
    # ──────────────────────────────────────────────────────────────────────────

    # Find the downloaded video file.
    #
    # We intentionally do NOT trust the original codec/container.
    # YouTube can give us AV1 + Opus, VP9 + Opus, etc.
    #
    # We will explicitly convert it to:
    #
    # H.264 + AAC + MP4

    video_extensions = {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
    }

    video_files = [
        file
        for file in files
        if file.suffix.lower() in video_extensions
    ]

    if not video_files:

        raise HTTPException(
            status_code=500,
            detail="No video file was produced.",
        )

    input_file = max(
        video_files,
        key=lambda file: file.stat().st_size,
    )

    filename = f"{title}.mp4"

    # If already a valid MP4 from yt-dlp, return directly without wasting CPU
    if input_file.suffix.lower() == ".mp4":
        return str(input_file), filename

    # Otherwise (e.g. .mkv or .webm), fast remux to MP4
    output_file = tmp_path / f"{title}_final.mp4"

    # Attempt 1: Instant stream copy (-c copy takes ~1 second and almost 0 CPU)
    remux_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            remux_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception:
        result = None

    if result and result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
        try:
            input_file.unlink()
        except Exception:
            pass
        return str(output_file), filename

    # Attempt 2: Copy video, convert audio to AAC (very fast, ~2-3 seconds)
    audio_transcode_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            audio_transcode_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except Exception:
        result = None

    if result and result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
        try:
            input_file.unlink()
        except Exception:
            pass
        return str(output_file), filename

    # Fallback: ultrafast video encoding
    ultrafast_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "25",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            ultrafast_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Video conversion failed: {exc}",
        ) from exc

    if not output_file.exists() or output_file.stat().st_size == 0:
        # If output_file wasn't created, return input_file as fallback
        return str(input_file), f"{title}{input_file.suffix}"

    try:
        input_file.unlink()
    except Exception:
        pass

    return str(output_file), filename


# ─── Error cleanup ─────────────────────────────────────────────────────────────

def _clean_ytdlp_error(msg: str) -> str:
    """Strip verbose yt-dlp prefix and traceback from errors."""

    msg = re.sub(
        r"ERROR:\s*\[[\w\s]+\]\s*[\w-]+:\s*",
        "",
        msg,
    )

    first_line = (
        msg.strip().splitlines()[0]
        if msg.strip()
        else "Unknown error"
    )

    return first_line[:300]


# ─── Cleanup task ─────────────────────────────────────────────────────────────

class _CleanupTask:
    """Background task that deletes the temporary directory."""

    def __init__(self, directory: str):
        self.directory = directory

    async def __call__(self):
        shutil.rmtree(
            self.directory,
            ignore_errors=True,
        )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)