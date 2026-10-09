import { useState } from 'react';
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

  const qualities = type === 'video' ? VIDEO_QUALITIES : AUDIO_QUALITIES;

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

    setStatus(STATUS.PROCESSING);

    try {
      setStatus(STATUS.DOWNLOADING);

      const response = await fetch('http://localhost:8000/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim(), type, quality }),
      });

      if (!response.ok) {
        let errData;
        try {
          errData = await response.json();
        } catch {
          errData = { detail: 'An unexpected error occurred. Please try again.' };
        }
        throw new Error(errData.detail || 'Download failed.');
      }

      setStatus(STATUS.PREPARING);

      const blob = await response.blob();
      const contentDisposition = response.headers.get('Content-Disposition') || '';
      let filename = type === 'audio' ? 'audio.mp3' : 'video.mp4';

      const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\r\n]+)/i);
      if (match) {
        filename = decodeURIComponent(match[1].replace(/['"]/g, ''));
      }

      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);

      setStatus(STATUS.DONE);
    } catch (err) {
      setErrorMsg(err.message || 'Something went wrong. Please try again.');
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
            </div>
          </div>

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
                {STATUS_MESSAGES[status]}
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

          {/* Status / Error */}
          {status !== STATUS.IDLE && !isLoading && (
            <div className={`status-bar ${status === STATUS.ERROR ? 'status-error' : status === STATUS.DONE ? 'status-done' : ''}`}>
              {status === STATUS.ERROR ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
                    <path d="M8 5v3.5M8 11v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                  {errorMsg}
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
                    <path d="M5 8l2.5 2.5L11 5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  {STATUS_MESSAGES[status]}
                </>
              )}
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
