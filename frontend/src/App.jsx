import { useState, useEffect } from 'react';
import './App.css';

const VIDEO_QUALITIES = [
  { value: 'best',  label: 'Best',  hint: 'highest' },
  { value: '1080',  label: '1080p', hint: 'FHD' },
  { value: '720',   label: '720p',  hint: 'HD' },
  { value: '480',   label: '480p',  hint: 'SD' },
  { value: '360',   label: '360p',  hint: 'low' },
];

const AUDIO_QUALITIES = [
  { value: 'best', label: 'Best',    hint: '~320k' },
  { value: '320',  label: '320kbps', hint: 'HQ' },
  { value: '192',  label: '192kbps', hint: 'standard' },
  { value: '128',  label: '128kbps', hint: 'small' },
];

const STATUS = {
  IDLE: 'idle',
  PROCESSING: 'processing',
  DOWNLOADING: 'downloading',
  PREPARING: 'preparing',
  DONE: 'done',
  ERROR: 'error',
};

const STATUS_MESSAGES = {
  [STATUS.IDLE]: 'Ready',
  [STATUS.PROCESSING]: 'Processing...',
  [STATUS.DOWNLOADING]: 'Downloading...',
  [STATUS.PREPARING]: 'Preparing file...',
  [STATUS.DONE]: 'Download completed ✓',
  [STATUS.ERROR]: '',
};

function extractVideoId(url) {
  if (!url) return null;
  const match = url.trim().match(/(?:youtu\.be\/|youtube\.com\/(?:embed\/|v\/|watch\?v=|watch\?.+&v=|shorts\/))([\w-]{11})/);
  return match ? match[1] : null;
}

function isValidYouTubeUrl(url) {
  const patterns = [
    /^https?:\/\/(www\.)?youtube\.com\/watch\?.*v=[\w-]+/,
    /^https?:\/\/youtu\.be\/[\w-]+/,
    /^https?:\/\/(www\.)?youtube\.com\/shorts\/[\w-]+/,
    /^https?:\/\/(www\.)?youtube\.com\/embed\/[\w-]+/,
  ];
  return patterns.some((p) => p.test(url.trim()));
}

export default function App() {
  const [url, setUrl] = useState('');
  const [type, setType] = useState('video');
  const [quality, setQuality] = useState('best');
  const [status, setStatus] = useState(STATUS.IDLE);
  const [errorMsg, setErrorMsg] = useState('');
  const [progress, setProgress] = useState(0);
  const [progressSpeed, setProgressSpeed] = useState('');
  const [progressEta, setProgressEta] = useState('');
  const [progressMessage, setProgressMessage] = useState('');
  const [videoPreview, setVideoPreview] = useState(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const qualities = type === 'video' ? VIDEO_QUALITIES : AUDIO_QUALITIES;

  // Instant preview when a valid YouTube link is entered/pasted
  useEffect(() => {
    const videoId = extractVideoId(url);
    if (!videoId) {
      setVideoPreview(null);
      setLoadingPreview(false);
      return;
    }

    const defaultThumb = `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`;
    setVideoPreview((prev) => {
      if (prev && prev.id === videoId) return prev;
      return {
        id: videoId,
        title: 'Loading video info...',
        author: '',
        thumbnail: defaultThumb,
      };
    });
    setLoadingPreview(true);

    const controller = new AbortController();
    fetch(`https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`, {
      signal: controller.signal,
    })
      .then((res) => {
        if (!res.ok) throw new Error('oEmbed unavailable');
        return res.json();
      })
      .then((data) => {
        setVideoPreview({
          id: videoId,
          title: data.title || 'YouTube Video',
          author: data.author_name || '',
          thumbnail: data.thumbnail_url || defaultThumb,
        });
        setLoadingPreview(false);
      })
      .catch((err) => {
        if (err.name !== 'AbortError') {
          setVideoPreview((prev) => (prev ? { ...prev, title: 'YouTube Video' } : null));
          setLoadingPreview(false);
        }
      });

    return () => controller.abort();
  }, [url]);

  const handlePaste = async () => {
    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        const text = await navigator.clipboard.readText();
        if (text) {
          const trimmed = text.trim();
          setUrl(trimmed);
          if (status === STATUS.ERROR || status === STATUS.DONE) setStatus(STATUS.IDLE);
        }
      }
    } catch (err) {
      console.warn('Clipboard read error:', err);
    }
  };

  const handleDownload = async () => {
    setErrorMsg('');

    if (!url.trim()) {
      setErrorMsg('Please enter a valid YouTube URL.');
      setStatus(STATUS.ERROR);
      return;
    }

    if (!isValidYouTubeUrl(url)) {
      setErrorMsg('Please enter a valid YouTube URL.');
      setStatus(STATUS.ERROR);
      return;
    }

    setProgress(0);
    setProgressSpeed('');
    setProgressEta('');
    setProgressMessage('Connecting to YouTube...');
    setStatus(STATUS.PROCESSING);

    try {
      if (typeof navigator !== 'undefined' && !navigator.onLine) {
        throw new Error('You are currently offline. Downloading media requires an active internet connection.');
      }

      const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');

      const startRes = await fetch(`${apiBaseUrl}/download/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim(), type, quality }),
      });

      if (!startRes.ok) {
        let errData;
        try {
          errData = await startRes.json();
        } catch {
          errData = { detail: 'An unexpected error occurred. Please try again.' };
        }
        throw new Error(errData.detail || 'Download failed to start.');
      }

      const { task_id: taskId } = await startRes.json();
      setStatus(STATUS.DOWNLOADING);

      // Poll progress every 300ms
      await new Promise((resolve, reject) => {
        const pollInterval = setInterval(async () => {
          try {
            const progRes = await fetch(`${apiBaseUrl}/download/progress/${taskId}`);
            if (!progRes.ok) {
              clearInterval(pollInterval);
              reject(new Error('Lost connection to download session.'));
              return;
            }

            const data = await progRes.json();

            if (typeof data.progress === 'number') {
              setProgress(data.progress);
            }
            if (data.speed) setProgressSpeed(data.speed);
            if (data.eta) setProgressEta(data.eta);
            if (data.message) setProgressMessage(data.message);

            if (data.status === 'processing') {
              setStatus(STATUS.PREPARING);
            } else if (data.status === 'complete') {
              clearInterval(pollInterval);
              setProgress(100);
              setProgressMessage('Download completed ✓');
              setStatus(STATUS.DONE);

              // Trigger direct file download
              const fileUrl = `${apiBaseUrl}/download/file/${taskId}`;
              const a = document.createElement('a');
              a.href = fileUrl;
              a.download = data.filename || (type === 'audio' ? 'audio.mp3' : 'video.mp4');
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);

              resolve();
            } else if (data.status === 'error') {
              clearInterval(pollInterval);
              reject(new Error(data.error || 'Download failed.'));
            }
          } catch (err) {
            clearInterval(pollInterval);
            reject(err);
          }
        }, 300);
      });
    } catch (err) {
      if (typeof navigator !== 'undefined' && !navigator.onLine) {
        setErrorMsg('You are offline. Downloading media requires an active internet connection.');
      } else if (err.name === 'TypeError') {
        setErrorMsg('Unable to connect to the backend server. Please verify your connection.');
      } else {
        setErrorMsg(err.message || 'Something went wrong. Please try again.');
      }
      setStatus(STATUS.ERROR);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') handleDownload();
  };

  const isLoading = [STATUS.PROCESSING, STATUS.DOWNLOADING, STATUS.PREPARING].includes(status);

  return (
    <div className="app">
      {/* Background decoration */}
      <div className="bg-glow bg-glow-1" />
      <div className="bg-glow bg-glow-2" />

      <div className="container">
        {/* Header */}
        <header className="header">
          <div className="logo">
            <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
              <rect width="36" height="36" rx="10" fill="#FFD600" />
              <polygon points="14,10 14,26 28,18" fill="#0D0D0D" />
            </svg>
          </div>
          <h1 className="title">YouTube Downloader</h1>
          <p className="subtitle">Download video or audio from a YouTube URL</p>
        </header>

        {/* Main card */}
        <main className="card">
          {/* URL Input */}
          <div className="input-group">
            <label className="input-label" htmlFor="youtube-url">
              YouTube URL
            </label>
            <div className="input-wrapper">
              <svg className="input-icon" width="20" height="20" viewBox="0 0 20 20" fill="none">
                <path d="M10 1.5C5.3 1.5 1.5 5.3 1.5 10S5.3 18.5 10 18.5 18.5 14.7 18.5 10 14.7 1.5 10 1.5zm5.2 6.4l-6.5 6.5c-.3.3-.8.3-1.1 0L4.8 11.5c-.3-.3-.3-.8 0-1.1.3-.3.8-.3 1.1 0l2.3 2.3 5.9-5.9c.3-.3.8-.3 1.1 0 .3.3.3.8 0 1.1z" fill="#FFD600" />
              </svg>
              <input
                id="youtube-url"
                type="url"
                className="url-input"
                placeholder="Paste YouTube URL here..."
                value={url}
                onChange={(e) => {
                  setUrl(e.target.value);
                  if (status === STATUS.ERROR || status === STATUS.DONE) setStatus(STATUS.IDLE);
                }}
                onKeyDown={handleKeyDown}
                disabled={isLoading}
                autoComplete="off"
                spellCheck={false}
              />
              <div className="input-actions">
                {url && (
                  <button
                    type="button"
                    className="input-action-btn clear-btn"
                    onClick={() => {
                      setUrl('');
                      setVideoPreview(null);
                      if (status === STATUS.ERROR || status === STATUS.DONE) setStatus(STATUS.IDLE);
                    }}
                    title="Clear input"
                    disabled={isLoading}
                    aria-label="Clear URL input"
                  >
                    <svg width="12" height="12" viewBox="0 0 14 14" fill="none">
                      <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                    </svg>
                  </button>
                )}
                <button
                  type="button"
                  className="input-action-btn paste-btn"
                  onClick={handlePaste}
                  title="Paste from clipboard"
                  disabled={isLoading}
                  aria-label="Paste from clipboard"
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
                    <path d="M5.5 2.5h5a1 1 0 011 1v1h1a1.5 1.5 0 011.5 1.5v7a1.5 1.5 0 01-1.5 1.5h-8A1.5 1.5 0 013 13V6a1.5 1.5 0 011.5-1.5h1v-1a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                    <path d="M5.5 4.5h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                  <span>Paste</span>
                </button>
              </div>
            </div>
          </div>

          {/* Instant Video Preview Card */}
          {videoPreview && (
            <div className="video-preview-card">
              <div className="preview-thumb-wrap">
                <img
                  src={videoPreview.thumbnail}
                  alt={videoPreview.title}
                  className="preview-thumb"
                  loading="eager"
                  onError={(e) => {
                    e.currentTarget.src = `https://img.youtube.com/vi/${videoPreview.id}/0.jpg`;
                  }}
                />
                <div className="preview-play-badge">
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                    <polygon points="4,2 4,12 12,7" fill="#0D0D0D" />
                  </svg>
                </div>
              </div>
              <div className="preview-details">
                <div className="preview-badge">
                  <span className="preview-pulse-dot" />
                  Ready to download
                </div>
                <h3 className="preview-title" title={videoPreview.title}>
                  {videoPreview.title}
                </h3>
                {videoPreview.author && (
                  <p className="preview-author">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                      <path d="M8 8a3 3 0 100-6 3 3 0 000 6zm-5 6a5 5 0 0110 0H3z" />
                    </svg>
                    {videoPreview.author}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Type selector */}
          <div className="type-group">
            <span className="input-label">Format</span>
            <div className="type-buttons" role="group" aria-label="Download format">
              <button
                id="type-video"
                className={`type-btn ${type === 'video' ? 'active' : ''}`}
                onClick={() => { setType('video'); setQuality('best'); }}
                disabled={isLoading}
                aria-pressed={type === 'video'}
              >
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                  <rect x="1" y="3" width="11" height="12" rx="2" stroke="currentColor" strokeWidth="1.5" />
                  <path d="M12 7l5-3v10l-5-3V7z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                </svg>
                Video
              </button>
              <button
                id="type-audio"
                className={`type-btn ${type === 'audio' ? 'active' : ''}`}
                onClick={() => { setType('audio'); setQuality('best'); }}
                disabled={isLoading}
                aria-pressed={type === 'audio'}
              >
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                  <path d="M9 1v10M6 4v4M12 3v6M3 7v2M15 6v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  <path d="M9 14a3 3 0 100-6 3 3 0 000 6z" stroke="currentColor" strokeWidth="1.5" />
                </svg>
                Audio (MP3)
              </button>
            </div>
          </div>

          {/* Quality selector */}
          <div className="quality-group">
            <span className="input-label">Quality</span>
            <div className="quality-pills" role="group" aria-label="Download quality">
              {qualities.map((q) => (
                <button
                  key={q.value}
                  id={`quality-${q.value}`}
                  className={`quality-pill ${quality === q.value ? 'active' : ''}`}
                  onClick={() => setQuality(q.value)}
                  disabled={isLoading}
                  aria-pressed={quality === q.value}
                  title={q.hint}
                >
                  {q.label}
                </button>
              ))}
            </div>
          </div>

          {/* Download button */}
          <button
            id="download-btn"
            className={`download-btn ${isLoading ? 'loading' : ''}`}
            onClick={handleDownload}
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <span className="spinner" />
                {progress > 0 ? `Downloading ${Math.round(progress)}%` : STATUS_MESSAGES[status]}
              </>
            ) : (
              <>
                <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
                  <path d="M10 2v11M6 9l4 4 4-4M3 16h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                Download
              </>
            )}
          </button>

          {/* Progress Card during download */}
          {isLoading && (
            <div className="progress-card">
              <div className="progress-header">
                <div className="progress-label">
                  <span className="progress-pulse" />
                  <span>{progressMessage || 'Downloading...'}</span>
                </div>
                <span className="progress-percentage">{Math.round(progress)}%</span>
              </div>

              <div className="progress-track">
                <div
                  className="progress-fill"
                  style={{ width: `${Math.max(progress, 3)}%` }}
                />
              </div>

              {(progressSpeed || progressEta) && (
                <div className="progress-footer">
                  {progressSpeed && <span>Speed: {progressSpeed}</span>}
                  {progressSpeed && progressEta && <span className="progress-separator">•</span>}
                  {progressEta && <span>ETA: {progressEta}</span>}
                </div>
              )}
            </div>
          )}

          {/* Success Status */}
          {!isLoading && status === STATUS.DONE && (
            <div className="status-bar status-done" aria-live="polite">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
                <path d="M5 8l2.5 2.5L11 5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Download completed! File is saving to your downloads.
            </div>
          )}

          {/* Error Status */}
          {!isLoading && status === STATUS.ERROR && (
            <div className="status-bar status-error" aria-live="polite">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
                <path d="M8 5v3.5M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              {errorMsg}
            </div>
          )}
        </main>

        {/* Tips */}
        <section className="tips">
          <p className="tips-title">Supported URLs</p>
          <ul className="tips-list">
            <li><code>https://www.youtube.com/watch?v=...</code></li>
            <li><code>https://youtu.be/...</code></li>
            <li><code>https://www.youtube.com/shorts/...</code></li>
          </ul>
        </section>

        <footer className="footer">
          <p>For personal use only. Respect copyright laws.</p>
        </footer>
      </div>
    </div>
  );
}
