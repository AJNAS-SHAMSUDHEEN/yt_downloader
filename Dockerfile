# Production Dockerfile for YouTube Downloader Backend (Repo Root)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install FFmpeg, curl, and Node.js for YouTube player JS signature decryption
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Deno binary
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

# Install dependencies from backend/requirements.txt
COPY backend/requirements.txt requirements.txt
RUN pip install --no-cache-dir "yt-dlp[default]>=2025.1.15" -r requirements.txt

# Copy application source
COPY backend/main.py main.py

# Expose default port
EXPOSE 8000

# Start server dynamically using platform PORT
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
