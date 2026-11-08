// Line icons drawn inline (no external asset), hidden from assistive technology.
export const ICONS = {
	dashboard: 'M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z',
	review: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h5',
	performance: 'M4 20h16M7 16V9M12 16V5M17 16v-4',
	locks: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5zM12 15v2',
	pause: 'M9 6v12M15 6v12',
	play: 'M8 5l11 7-11 7z',
	chevron: 'M9 6l6 6-6 6',
	plus: 'M12 5v14M5 12h14',
	arrowDown: 'M12 6v10M7.5 11.5 12 16l4.5-4.5M7 19h10',
	check: 'M6.5 12.5l3.5 3.5 7.5-8',
	alert: 'M12 7.5v5.5M12 16.5v.5',
	external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
} as const;

export function Icon({ name, className = 'icon' }: { name: keyof typeof ICONS; className?: string }) {
	return <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className={className}><path d={ICONS[name]} /></svg>;
}

export function BrandMark({ className = 'brand-mark' }: { className?: string }) {
	return (
		<svg viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
			<ellipse cx="13" cy="8" rx="8" ry="3" className="bm-top" />
			<path d="M5 8v15M21 8v7.5M5 15.5a8 3 0 0 0 16 0" className="bm-ink" />
			<path d="M5 23a8 3 0 0 0 8 3l14-15" className="bm-check" />
		</svg>
	);
}
