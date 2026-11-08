import { useEffect, useState } from 'react';
import { applyThemeChoice, prefersDark, readThemeChoice, saveThemeChoice, type ThemeChoice } from './theme';
import { SqlWorkspace, type WorkspaceState, initialWorkspace } from './SqlWorkspace';

type View = 'dashboard' | 'review' | 'performance' | 'locks';

const NAV: { id: View; label: string; icon: string }[] = [
	{ id: 'dashboard', label: 'Dashboard', icon: 'M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z' },
	{ id: 'review', label: 'SQL review', icon: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h5' },
	{ id: 'performance', label: 'Observed performance', icon: 'M4 20h16M7 16V9M12 16V5M17 16v-4' },
	{ id: 'locks', label: 'Lock diagnostics', icon: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5zM12 15v2' },
];

const AMBIENT_KEY = 'mdv-ambient';
function readAmbient(): boolean {
	try { return localStorage.getItem(AMBIENT_KEY) !== 'paused'; } catch { return true; }
}

function Icon({ d }: { d: string }) {
	return <svg viewBox="0 0 24 24" aria-hidden="true" className="icon"><path d={d} /></svg>;
}

export function App() {
	const [view, setView] = useState<View>('review');
	const [theme, setTheme] = useState<ThemeChoice>(readThemeChoice);
	const [ambient, setAmbient] = useState(readAmbient);
	const [workspace, setWorkspace] = useState<WorkspaceState>(initialWorkspace);

	useEffect(() => {
		applyThemeChoice(theme);
		if (theme !== 'system') return;
		const query = window.matchMedia('(prefers-color-scheme: dark)');
		const follow = () => applyThemeChoice('system');
		query.addEventListener('change', follow);
		return () => query.removeEventListener('change', follow);
	}, [theme]);

	const chooseTheme = (choice: ThemeChoice) => { setTheme(choice); saveThemeChoice(choice); };
	const toggleAmbient = () => {
		const next = !ambient;
		setAmbient(next);
		try { localStorage.setItem(AMBIENT_KEY, next ? 'on' : 'paused'); } catch { /* this page only */ }
	};

	return (
		<div className={'app' + (ambient ? '' : ' ambient-paused')}>
			<div className="ambient" aria-hidden="true">
				<span className="glow glow-a" /><span className="glow glow-b" /><span className="glow glow-c" />
			</div>
			<aside className="sidebar">
				<div className="brand">
					<svg viewBox="0 0 32 32" className="brand-mark" aria-hidden="true">
						<ellipse cx="13" cy="8" rx="8" ry="3" className="bm-top" />
						<path d="M5 8v15M21 8v7.5M5 15.5a8 3 0 0 0 16 0" className="bm-ink" />
						<path d="M5 23a8 3 0 0 0 8 3l14-15" className="bm-check" />
					</svg>
					<div><strong>MySQL DBA Validator</strong><span>Pre-execution SQL review</span></div>
				</div>
				<nav aria-label="Views">
					{NAV.map(item => (
						<button key={item.id} type="button" className="nav-item" aria-current={view === item.id ? 'page' : undefined}
							onClick={() => setView(item.id)}>
							<Icon d={item.icon} />{item.label}
						</button>
					))}
				</nav>
				<div className="sidebar-foot">
					<div className="segmented" role="radiogroup" aria-label="Theme">
						{(['light', 'dark', 'system'] as const).map(choice => (
							<button key={choice} type="button" role="radio" aria-checked={theme === choice}
								onClick={() => chooseTheme(choice)}>
								{choice === 'system' ? `System (${prefersDark() ? 'dark' : 'light'})` : choice[0]!.toUpperCase() + choice.slice(1)}
							</button>
						))}
					</div>
					<button type="button" className="btn btn-quiet btn-sm ambient-toggle" aria-pressed={!ambient} onClick={toggleAmbient}>
						<Icon d={ambient ? 'M9 6v12M15 6v12' : 'M8 5l11 7-11 7z'} />
						{ambient ? 'Pause background motion' : 'Resume background motion'}
					</button>
				</div>
			</aside>
			<main className="main" key={view}>
				{view === 'review'
					? <SqlWorkspace state={workspace} setState={setWorkspace} />
					: <Pending label={NAV.find(n => n.id === view)!.label} onBack={() => setView('review')} />}
			</main>
		</div>
	);
}

function Pending({ label, onBack }: { label: string; onBack: () => void }) {
	return (
		<section className="view pending">
			<header className="page-head"><h1>{label}</h1></header>
			<div className="panel pending-body">
				<p>This view is not part of the prototype. It stays in the current application until the visual
					direction is approved.</p>
				<button type="button" className="btn btn-secondary" onClick={onBack}>Back to SQL review</button>
			</div>
		</section>
	);
}
