
import os
import re
import shutil
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


# ─── Constants ────────────────────────────────────────────────────────────────

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
    """Check if the URL matches known YouTube URL patterns."""

    if not url or len(url) > MAX_URL_LENGTH:
        return False

    return any(
        pattern.match(url.strip())
        for pattern in YOUTUBE_URL_PATTERNS
    )


def check_ffmpeg() -> bool:
    """Return True if ffmpeg is available on PATH."""

    return shutil.which("ffmpeg") is not None


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

    # 2. Check FFmpeg availability

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


# ─── Download logic ────────────────────────────────────────────────────────────


async def _do_download(
    url: str,
    download_type: str,
    quality: str,
    tmp_dir: str,
):
    """Download media with yt-dlp and return (path, filename)."""

    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s",
    )

    # ──────────────────────────────────────────────────────────────────────────
    # AUDIO
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

            "quiet": True,
            "no_warnings": True,

            "socket_timeout": DOWNLOAD_TIMEOUT,

            "retries": 3,

            "noplaylist": True,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # VIDEO
    # ──────────────────────────────────────────────────────────────────────────

    else:

        # Best quality
        #
        # bv* = best available video format
        # ba  = best available audio format
        # b   = fallback combined format

        if quality == "best":

            fmt = "bv*+ba/b"

        else:

            height = int(quality)

            fmt = (
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]/"
                f"bv*+ba/b"
            )

        ydl_opts = {
            "format": fmt,

            "outtmpl": outtmpl,

            # Ask FFmpeg to merge into MP4 where possible.
            "merge_output_format": "mp4",

            "quiet": True,
            "no_warnings": True,

            "socket_timeout": DOWNLOAD_TIMEOUT,

            "retries": 3,

            "noplaylist": True,
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

    # Prefer the largest media file.

    output_file = max(
        files,
        key=lambda file: file.stat().st_size,
    )

    title = sanitize_filename(
        info.get(
            "title",
            "download",
        )
    )

    ext = output_file.suffix

    filename = f"{title}{ext}"

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