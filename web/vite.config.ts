import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The built page is served by FastAPI from frontend/app/ at /static/app/; in
// development, Vite proxies /api to a FastAPI started from this worktree on 8422
// (never the installed app on 8420).
export default defineConfig({
	plugins: [react()],
	base: '/static/app/',
	// Fonts and images are emitted as files, never data: URLs: the page's CSP
	// (default-src 'self') allows only same-origin fonts.
	build: { outDir: '../frontend/app', emptyOutDir: true, sourcemap: false, assetsInlineLimit: 0 },
	server: {
		host: '127.0.0.1',
		port: 5173,
		strictPort: true,
		proxy: { '/api': 'http://127.0.0.1:8422' },
	},
});
