import { defineConfig } from 'vite';
import { VitePWA } from 'vite-plugin-pwa';

export default defineConfig({
  plugins: [
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: [
        'favicon.svg',
        'apple-touch-icon.png',
        'pwa-192x192.png',
        'pwa-512x512.png',
        'maskable-icon-512x512.png',
      ],
      manifest: {
        name: 'YouTube Downloader',
        short_name: 'YT Downloader',
        description: 'Download YouTube videos and audio as MP3 for free. Fast, simple, no sign-up required.',
        theme_color: '#0D0D0D',
        background_color: '#0D0D0D',
        display: 'standalone',
        orientation: 'portrait-primary',
        start_url: '/',
        scope: '/',
        icons: [
          {
            src: '/pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: '/pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
          },
          {
            src: '/maskable-icon-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        // App shell and static asset caching only
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff,woff2}'],
        navigateFallback: '/index.html',
        // Do NOT cache API endpoints or media downloads
        navigateFallbackDenylist: [/^\/download/, /^\/health/, /^https?:\/\/.*\/download/],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.includes('/download') || url.pathname.includes('/health'),
            handler: 'NetworkOnly',
          },
        ],
      },
    }),
  ],
});
