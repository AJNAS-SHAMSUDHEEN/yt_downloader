# Production Dockerfile for YouTube Downloader Backend with FFmpeg and modern JS Runtimes
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

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py .

# Expose default port
EXPOSE 8000

# Start server dynamically using platform PORT
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
