// Browser storage for per-viewer conveniences only: the theme word, the ambient-light
// choice, the editor split and update versions. Never a login, a token, SQL or a
// database name. Storage may be unavailable (private window, blocked site data): every
// read falls back and every write is best effort.

export function readStored(key: string): string | null {
	try { return localStorage.getItem(key); } catch { return null; }
}

export function writeStored(key: string, value: string): void {
	try { localStorage.setItem(key, value); } catch { /* kept for this page only */ }
}

export function removeStored(key: string): void {
	try { localStorage.removeItem(key); } catch { /* nothing stored */ }
}
