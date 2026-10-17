# Deployment & Progressive Web App (PWA) Guide

This guide details how to deploy the YouTube Downloader application publicly to **Render** (or similar platforms) and how users can install it as a Progressive Web App (PWA) across Android, iOS, Windows, and macOS.

---

## Architecture Overview

- **Frontend:** React + Vite PWA (Static Site)
  - Pre-caches the app shell (HTML, CSS, JS, branding icons) for instant loads.
  - Automatically updates when new versions are deployed without caching stale bundles.
  - Never caches media downloads or API responses.
- **Backend:** Python 3.12 + FastAPI + yt-dlp + FFmpeg (Docker Web Service)
  - Bundles FFmpeg inside a lightweight container to guarantee codec support (H.264/AAC MP4).
  - Listens on `0.0.0.0` and binds dynamically to the platform's `$PORT`.
  - Configurable CORS via `ALLOWED_ORIGINS`.
  - Concurrency control and URL scheme/host filtering for security.

---

## 1. Deploy the Backend to Render (with FFmpeg)

Because FFmpeg is required for combining high-definition video/audio streams and converting to universal H.264/AAC MP4, deploying via the provided **Dockerfile** is the most reliable method on Render.

### Step-by-step:
1. Log in to [Render Dashboard](https://dashboard.render.com/).
2. Click **New +** and select **Web Service**.
3. Connect your GitHub repository: `https://github.com/AJNAS-SHAMSUDHEEN/yt_downloader`.
4. Configure the service settings:
   - **Name:** `yt-downloader-backend` (or your preferred name)
   - **Region:** Choose the region closest to you (e.g., Oregon, Frankfurt, Singapore)
   - **Branch:** `main`
   - **Root Directory:** `backend`
   - **Runtime:** **Docker** (Render will automatically detect `backend/Dockerfile`)
   - **Instance Type:** **Free** (or Starter for higher memory/no sleep)
5. Under **Environment Variables**, add:
   | Key | Value | Notes |
   |---|---|---|
   | `ALLOWED_ORIGINS` | `https://<your-frontend-name>.onrender.com` | Replace with your frontend URL once created. You can temporarily leave blank or set to `*` during initial testing. |
   | `MAX_CONCURRENT_DOWNLOADS` | `2` | Protects RAM on free instances. |
6. Click **Create Web Service**.
7. Render will build the Docker container (installing FFmpeg and Python packages). Once live, note your backend URL:
   `https://yt-downloader-backend-xxxx.onrender.com`
8. Verify backend health by opening `https://<your-backend-url>/health` in your browser:
   ```json
   {
     "status": "ok",
     "ffmpeg": true,
     "ffprobe": true
   }
   ```

---

## 2. Deploy the Frontend to Vercel

1. Log in to [Vercel Dashboard](https://vercel.com/dashboard).
2. Click **Add New…** ➔ **Project**.
3. Import your GitHub repository (`yt_downloader`).
4. Configure the project settings:
   - **Framework Preset:** `Vite`
   - **Root Directory:** Click `Edit` and select `frontend`
   - **Build Command:** `npm run build` (default)
   - **Output Directory:** `dist` (default)
   - **Install Command:** `npm install` (default)
5. Under **Environment Variables**, add:
   | Key | Value | Notes |
   |---|---|---|
   | `VITE_API_BASE_URL` | `https://<your-backend-url>.onrender.com` | Your Render backend URL from Step 1 |
6. Click **Deploy**.
7. Once deployed, copy your production Vercel URL (e.g., `https://yt-downloader-xxxx.vercel.app`).
8. **Final Step (CORS Connection):**
   - Go back to your [Render Dashboard](https://dashboard.render.com/) ➔ Your Backend Web Service ➔ **Environment**.
   - Set `ALLOWED_ORIGINS` to your Vercel URL (e.g. `https://yt-downloader-xxxx.vercel.app`).
   - Save changes. Render will automatically redeploy with the updated CORS policy.

---

## 3. PWA Installation Instructions

> **Note on PWA Requirements:** Web browsers require an **HTTPS** URL (or `localhost` for development) and a registered service worker with valid icons for PWA installation.

### Android (Google Chrome)
1. Open your live HTTPS site in Chrome.
2. An **"Add to Home screen"** or **"Install app"** banner will appear at the bottom.
3. If not prompted automatically, tap the three-dot menu (**⋮**) in the top-right corner and select **Install app** (or **Add to Home screen**).
4. The app will install with its custom yellow play-button icon and open in standalone fullscreen mode.

### iOS / iPadOS (Safari)
1. Open the website in **Safari** (iOS requires Safari for PWA installation).
2. Tap the **Share** button (box with an upward arrow at the bottom of the screen).
3. Scroll down and tap **Add to Home Screen**.
4. Tap **Add** in the upper right. The app icon will appear on your iOS home screen and open without Safari browser chrome.

### Desktop (Google Chrome / Microsoft Edge)
1. Open the website in Chrome or Edge.
2. An **Install** icon (computer screen with a down arrow) will appear in the right side of the address bar.
3. Click the icon, or open the browser menu (**⋮**) and choose **Install YouTube Downloader**.
4. The application will launch in its own dedicated window with native desktop taskbar integration.

---

## 4. Testing End-to-End Download

1. Open the installed PWA or website.
2. Paste a standard YouTube video URL (e.g., `https://www.youtube.com/watch?v=...` or `https://youtu.be/...`).
3. Select **Video** or **Audio (MP3)** and pick your desired quality.
4. Click **Download**.
5. The status will transition:
   `Downloading...` ➔ `Preparing file...` ➔ `Download complete`
6. The MP4 video or MP3 audio file will trigger an automatic download to your device's downloads folder.

---

## 5. Troubleshooting & Operations

### 1. Free Tier Inactivity (Sleeping Backend)
- **Symptom:** The first download request takes 30–50 seconds or fails with a timeout.
- **Cause:** Render free web services spin down after 15 minutes of inactivity.
- **Fix:** Wait 30 seconds for the backend instance to spin up, or ping `/health` before downloading.

### 2. CORS Errors
- **Symptom:** Browser console shows `Access to fetch at ... blocked by CORS policy`.
- **Cause:** `ALLOWED_ORIGINS` on the backend does not match your frontend URL.
- **Fix:** In Render backend settings, ensure `ALLOWED_ORIGINS` includes your exact frontend domain (including `https://` without a trailing slash).

### 3. Service Worker Updates
- The service worker is configured with `registerType: 'autoUpdate'`. When a new build is deployed, clients will fetch and activate the new bundle automatically on the next visit.
- To force an immediate update locally, open DevTools ➔ **Application** ➔ **Service Workers** ➔ click **Update** or **Unregister**.

### 4. YouTube Video Rate Limits or Restrictions
- Age-restricted, private, or members-only videos cannot be downloaded.
- If YouTube blocks datacenter IP addresses, updating yt-dlp to the latest version (`pip install --upgrade yt-dlp`) or using a dedicated server IP resolves the issue.
