import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Two pages, built into frontend/app/ (committed, so the server, the tests and the
// packaged apps need no Node): index.html, the workspace (React), and home.html, the
// public homepage (TypeScript and CSS only). FastAPI serves them; their hashed files
// are under /static/app/assets/. Images that are not part of the build (favicon,
// screenshots, the fish) stay in frontend/assets/ at /static/assets/.
//
// In development, Vite proxies /api to a FastAPI started from this worktree on 8422
// (never the installed app on 8420).
export default defineConfig({
	plugins: [react()],
	base: '/static/app/',
	publicDir: false,
	build: {
		outDir: '../frontend/app',
		emptyOutDir: true,
		sourcemap: false,
		// Fonts and images are files, never data: URLs (the CSP allows same-origin fonts only).
		assetsInlineLimit: 0,
		chunkSizeWarningLimit: 1500,
		// Every supported browser has modulepreload; no polyfill chunk.
		modulePreload: { polyfill: false },
		rolldownOptions: {
			input: { index: resolve(import.meta.dirname, 'index.html'), home: resolve(import.meta.dirname, 'home.html') },
		},
	},
	server: {
		host: '127.0.0.1',
		port: 5173,
		strictPort: true,
		proxy: { '/api': 'http://127.0.0.1:8422' },
	},
});
