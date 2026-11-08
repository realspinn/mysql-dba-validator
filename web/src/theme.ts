// Light, Dark or System. The choice uses the existing page's key (mdv-theme), so a
// saved preference carries over. Cloudscape is themed only through its supported
// tokens, for the one control it provides here (the database Select).
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { applyTheme, type Theme } from '@cloudscape-design/components/theming';

export type ThemeChoice = 'light' | 'dark' | 'system';
const KEY = 'mdv-theme';

export function readThemeChoice(): ThemeChoice {
	try {
		const stored = localStorage.getItem(KEY);
		if (stored === 'light' || stored === 'dark' || stored === 'system') return stored;
	} catch { /* storage unavailable: use the system setting */ }
	return 'system';
}

export function saveThemeChoice(choice: ThemeChoice): void {
	try { localStorage.setItem(KEY, choice); } catch { /* kept for this page only */ }
}

export const prefersDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches;

export function applyThemeChoice(choice: ThemeChoice): void {
	const dark = choice === 'dark' || (choice === 'system' && prefersDark());
	document.documentElement.dataset.theme = dark ? 'dark' : 'light';
	applyMode(dark ? Mode.Dark : Mode.Light);
}

const FONT = '"Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif';

const CLOUDSCAPE_THEME: Theme = {
	tokens: {
		fontFamilyBase: FONT,
		borderRadiusInput: '8px',
		borderRadiusDropdown: '10px',
		borderRadiusItem: '6px',
		colorBackgroundInputDefault: { light: '#ffffff', dark: '#17181c' },
		colorBorderInputDefault: { light: '#c9c5bd', dark: '#3a3b41' },
		colorBorderInputFocused: { light: '#b9621f', dark: '#f0a35e' },
		colorBorderItemFocused: { light: '#b9621f', dark: '#f0a35e' },
		colorBorderDropdownItemFocused: { light: '#b9621f', dark: '#f0a35e' },
		colorBackgroundDropdownItemDefault: { light: '#ffffff', dark: '#17181c' },
		colorBackgroundDropdownItemHover: { light: '#f5efe7', dark: '#24221f' },
		colorBorderDropdownItemHover: { light: '#e6d8c8', dark: '#3a342c' },
		colorBackgroundDropdownItemSelected: { light: '#fbf1e6', dark: '#2b241c' },
		colorBorderItemSelected: { light: '#d9a06a', dark: '#a8703f' },
		colorBackgroundItemSelected: { light: '#fbf1e6', dark: '#2b241c' },
		colorTextAccent: { light: '#a4561b', dark: '#f0a35e' },
		colorTextInteractiveDefault: { light: '#4a4843', dark: '#b7b5af' },
		colorTextInteractiveHover: { light: '#18171b', dark: '#edece9' },
		colorTextInteractiveActive: { light: '#a4561b', dark: '#f0a35e' },
		colorTextInputPlaceholder: { light: '#6c6a64', dark: '#8b8984' },
		colorTextButtonInlineIconDefault: { light: '#4a4843', dark: '#b7b5af' },
		colorTextButtonInlineIconHover: { light: '#a4561b', dark: '#f0a35e' },
		colorTextBodyDefault: { light: '#18171b', dark: '#edece9' },
		colorBackgroundInputDisabled: { light: '#f1efeb', dark: '#141518' },
		colorBorderDropdownContainer: { light: '#d8d3ca', dark: '#34353b' },
		colorTextDropdownItemHighlighted: { light: '#18171b', dark: '#ffffff' },
		colorBackgroundDropdownItemFilterMatch: { light: '#f8e3cc', dark: '#3d2f20' },
		colorTextDropdownItemFilterMatch: { light: '#6b3610', dark: '#ffd2a8' },
	},
};

let applied = false;
export function themeCloudscape(): void {
	if (applied) return;
	applied = true;
	applyTheme({ theme: CLOUDSCAPE_THEME });
}
