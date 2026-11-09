// Cloudscape provides one control here, the database Select. It is themed only through
// its supported design tokens (never its internal class names or DOM), so it matches
// the custom controls in styles/controls.css.
import { applyTheme, type Theme } from '@cloudscape-design/components/theming';

const FONT = '"Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif';
const ACCENT = { light: '#b9621f', dark: '#f0a35e' };
const ACCENT_TEXT = { light: '#a4561b', dark: '#f0a35e' };
const TEXT = { light: '#18171b', dark: '#edece9' };
const TEXT_2 = { light: '#4a4843', dark: '#b7b5af' };

const THEME: Theme = {
	tokens: {
		fontFamilyBase: FONT,
		borderRadiusInput: '8px',
		borderRadiusDropdown: '10px',
		borderRadiusItem: '6px',
		colorBackgroundInputDefault: { light: '#ffffff', dark: '#17181c' },
		colorBorderInputDefault: { light: '#c9c5bd', dark: '#3a3b41' },
		colorBorderInputFocused: ACCENT,
		colorBorderItemFocused: ACCENT,
		colorBorderDropdownItemFocused: ACCENT,
		colorBackgroundDropdownItemDefault: { light: '#ffffff', dark: '#17181c' },
		colorBackgroundDropdownItemHover: { light: '#f5efe7', dark: '#24221f' },
		colorBorderDropdownItemHover: { light: '#e6d8c8', dark: '#3a342c' },
		colorBackgroundDropdownItemSelected: { light: '#fbf1e6', dark: '#2b241c' },
		colorBorderItemSelected: { light: '#d9a06a', dark: '#a8703f' },
		colorBackgroundItemSelected: { light: '#fbf1e6', dark: '#2b241c' },
		colorTextAccent: ACCENT_TEXT,
		colorTextInteractiveDefault: TEXT_2,
		colorTextInteractiveHover: TEXT,
		colorTextInteractiveActive: ACCENT_TEXT,
		colorTextInputPlaceholder: { light: '#6c6a64', dark: '#8b8984' },
		colorTextButtonInlineIconDefault: TEXT_2,
		colorTextButtonInlineIconHover: ACCENT_TEXT,
		colorTextBodyDefault: TEXT,
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
	applyTheme({ theme: THEME });
}
