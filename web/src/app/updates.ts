// Update notice rules. The backend checks for a newer official release at most once per
// start (backend/update_check.py); the page only asks its own server, and offers only a
// newer version whose link is the official release page. Only version numbers are
// stored: the one dismissed with "Later", and the one being installed (so the new copy
// can confirm it is running).
import { readStored, removeStored, writeStored } from '../shared/storage';

export const UPDATE_VERSION = /^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$/;
export const RELEASE_PAGE = 'https://github.com/realspinn/mysql-dba-validator/releases/tag/v';
const RELEASE_DOWNLOAD = 'https://github.com/realspinn/mysql-dba-validator/releases/download/v';
const UPDATE_BUILDS: Record<string, string> = { 'Windows x64': 'windows-x64', 'macOS Apple Silicon': 'macos-arm64-unsigned' };
const DISMISSED = 'mdv-update-dismissed';
const PENDING = 'mdv-update-pending';

const readVersion = (key: string) => {
	const stored = readStored(key);
	return stored && UPDATE_VERSION.test(stored) ? stored : '';
};
const saveVersion = (key: string, version: string) => { if (UPDATE_VERSION.test(version)) writeStored(key, version); };

export const readDismissed = () => readVersion(DISMISSED);
export const saveDismissed = (version: string) => saveVersion(DISMISSED, version);
export const savePending = (version: string) => saveVersion(PENDING, version);

export interface DownloadOffer { name: string; url: string; size: number; platform: string; inApp: boolean }
export interface Offer { version: string; current: string; download: DownloadOffer | null }

// Only the official file for this exact version and a known build.
export function downloadOffer(download: unknown, version: string): DownloadOffer | null {
	if (!download || typeof download !== 'object') return null;
	const d = download as Record<string, unknown>;
	const platform = typeof d.platform === 'string' ? d.platform : '';
	if (!Object.hasOwn(UPDATE_BUILDS, platform)) return null;
	const name = 'MySQL-DBA-Validator-v' + version + '-' + UPDATE_BUILDS[platform] + '.zip';
	if (d.name !== name || d.url !== RELEASE_DOWNLOAD + version + '/' + name
		|| !Number.isSafeInteger(d.size) || (d.size as number) <= 0) return null;
	return { name, url: d.url as string, size: d.size as number, platform, inApp: d.in_app === true && platform === 'Windows x64' };
}

const parts = (version: string) => version.split('.').map(Number);
export function versionBefore(a: string, b: string): boolean {
	const [x, y] = [parts(a), parts(b)];
	for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i]! < y[i]!;
	return false;
}

// After an update the new copy confirms the version it is running. An older copy,
// opened again before the new one is installed, leaves the pending version alone.
export function confirmUpdated(current: string): boolean {
	const pending = readVersion(PENDING);
	if (!pending || !current || versionBefore(current, pending)) return false;
	removeStored(PENDING);
	return pending === current;
}

export const megabytes = (bytes: number) => (bytes / 1e6).toFixed(1) + ' MB';
