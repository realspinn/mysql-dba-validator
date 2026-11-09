// Light, Dark or System. The workspace keeps the choice under 'mdv-theme' (as the
// previous page did, so a saved choice carries over). The HTML entry sets data-theme
// before first paint; this module keeps it, and Cloudscape's mode, in step.
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { readStored, writeStored } from './storage';

export type ThemeChoice = 'light' | 'dark' | 'system';
export const THEME_CHOICES: readonly ThemeChoice[] = ['light', 'dark', 'system'];
const KEY = 'mdv-theme';

const isChoice = (value: string | null): value is ThemeChoice =>
	value === 'light' || value === 'dark' || value === 'system';

export function readThemeChoice(): ThemeChoice {
	const stored = readStored(KEY);
	return isChoice(stored) ? stored : 'system';
}

export function saveThemeChoice(choice: ThemeChoice): void {
	writeStored(KEY, choice);
}

export const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches;

export function applyThemeChoice(choice: ThemeChoice): void {
	const dark = choice === 'dark' || (choice === 'system' && prefersDark());
	document.documentElement.dataset.theme = dark ? 'dark' : 'light';
	applyMode(dark ? Mode.Dark : Mode.Light);
}
