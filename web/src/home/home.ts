// The public homepage: theme choice, the two menus, the screenshot fade-in and the fish.
// Static content needs no framework: it shares the workspace's tokens, controls and
// ambient light (home.css). It stores only the theme word and makes no requests.
import './home.css';

const KEY = 'mdv-home-theme';
type Theme = 'light' | 'dark' | 'system';
const root = document.documentElement;
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

// Theme: Light, Dark or System (the default, which follows the OS, also when it changes).
let choice: Theme = 'system';
try {
	const stored = localStorage.getItem(KEY);
	if (stored === 'light' || stored === 'dark' || stored === 'system') choice = stored;
} catch { /* storage unavailable: this page only */ }
const apply = () => root.setAttribute('data-theme', choice === 'dark' || (choice === 'system' && darkQuery.matches) ? 'dark' : 'light');
apply();
darkQuery.addEventListener('change', () => { if (choice === 'system') apply(); });
document.querySelectorAll<HTMLInputElement>('input[name="theme"]').forEach(radio => {
	radio.checked = radio.value === choice;
	radio.addEventListener('change', () => {
		const value = radio.value;
		if (!radio.checked || (value !== 'light' && value !== 'dark' && value !== 'system')) return;
		choice = value;
		apply();
		try { localStorage.setItem(KEY, value); } catch { /* this page only */ }
	});
});

// The menus are details elements, which open and close without script. This adds: a
// click outside or Escape closes them (Escape returns focus to the trigger), choosing
// with the pointer closes the menu, and the arrow keys move through the download links.
const menus = [...document.querySelectorAll<HTMLDetailsElement>('details.menu')];
document.addEventListener('click', event => {
	const menu = (event.target as Element).closest('details.menu');
	menus.forEach(m => { if (m !== menu) m.open = false; });
	// event.detail is 0 for a keyboard "click", so Space on a theme radio keeps it open.
	if (menu instanceof HTMLDetailsElement && event.detail && (event.target as Element).closest('.menu-panel')) menu.open = false;
});
document.addEventListener('keydown', event => {
	const target = event.target as Element;
	const menu = target.closest?.('details.menu');
	if (event.key === 'Escape') {
		menus.forEach(m => {
			if (!m.open) return;
			m.open = false;
			if (m.contains(document.activeElement)) m.querySelector('summary')?.focus();
		});
		return;
	}
	if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains('download')) return;
	if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
	event.preventDefault();
	menu.open = true;
	const items = [...menu.querySelectorAll<HTMLAnchorElement>('.menu-panel a')];
	const at = items.indexOf(document.activeElement as HTMLAnchorElement);
	const next = event.key === 'ArrowDown' ? at + 1 : (at < 0 ? items.length : at) - 1;
	items[(next + items.length) % items.length]?.focus();
});

// The screenshot fades in when its image arrives.
document.querySelectorAll<HTMLImageElement>('.frame img').forEach(shot => {
	if (shot.complete && shot.naturalWidth) return;
	shot.classList.add('pending');
	const shown = () => shot.classList.remove('pending');
	shot.addEventListener('load', shown);
	shot.addEventListener('error', shown);
});

// The fish swims down the right margin as the page scrolls down, and back up as it
// scrolls up: one passive listener, at most one update per frame, transform and class
// changes only. Purely decorative, and still under reduced motion.
const fish = document.querySelector<HTMLElement>('.fish');
if (fish) {
	let lastY = window.scrollY;
	let queued = false;
	let idle = 0;
	const place = () => {
		queued = false;
		const y = window.scrollY;
		const room = document.documentElement.scrollHeight - window.innerHeight;
		const still = reduce.matches;
		const p = still || room <= 0 ? 0 : Math.min(1, Math.max(0, y / room));
		const top = 96;
		const bottom = Math.max(top, window.innerHeight - 136);
		const drift = still ? 0 : Math.sin(p * Math.PI * 3) * 6;
		fish.style.transform = `translate3d(${drift.toFixed(1)}px,${Math.round(top + p * (bottom - top))}px,0)`;
		if (!still && y !== lastY) {
			fish.classList.toggle('down', y > lastY);
			fish.classList.toggle('up', y < lastY);
			fish.classList.add('swim');
			window.clearTimeout(idle);
			idle = window.setTimeout(() => fish.classList.remove('down', 'up', 'swim'), 350);
		}
		lastY = y;
	};
	const schedule = () => { if (!queued) { queued = true; window.requestAnimationFrame(place); } };
	window.addEventListener('scroll', schedule, { passive: true });
	window.addEventListener('resize', schedule);
	place();
	window.addEventListener('load', () => window.setTimeout(() => fish.classList.add('in'), 300));
}
