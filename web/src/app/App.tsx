// The workspace shell: navigation, theme, the update notice and the four views. The
// review and the connection are shared by all views and kept while switching.
import { useEffect, useRef, useState } from 'react';
import { targetName } from '../api/connector';
import { PUBLIC_DEMO } from '../shared/environment';
import { readStored, writeStored } from '../shared/storage';
import { applyThemeChoice, readThemeChoice, saveThemeChoice, type ThemeChoice } from '../shared/theme';
import { ConnectionProvider, useConnectionContext } from '../features/connection/ConnectionContext';
import { usableDatabase } from '../features/connection/connectionState';
import { DashboardView } from '../features/dashboard/DashboardView';
import { SnapshotView } from '../features/diagnostics/SnapshotView';
import { LOCKS, PERFORMANCE } from '../features/diagnostics/snapshotText';
import { ReviewView } from '../features/review/ReviewView';
import type { FocusRequest } from '../features/review/StatementList';
import { useReview } from '../features/review/useReview';
import { AmbientLight } from './AmbientLight';
import { Sidebar, type View } from './Sidebar';
import { UpdateNotice } from './UpdateNotice';
import { useHeartbeat } from './useHeartbeat';

// The online demo has no database, so no diagnostics views.
const VIEWS: View[] = PUBLIC_DEMO ? ['dashboard', 'review'] : ['dashboard', 'review', 'performance', 'locks'];
const AMBIENT_KEY = 'mdv-ambient';

export function App() {
	return <ConnectionProvider><Workspace /></ConnectionProvider>;
}

function Workspace() {
	const { state, actions } = useConnectionContext();
	const review = useReview(actions);
	const [view, setView] = useState<View>('dashboard');
	const [theme, setTheme] = useState<ThemeChoice>(readThemeChoice);
	const [ambient, setAmbient] = useState(() => readStored(AMBIENT_KEY) !== 'paused');
	const [settingsOpen, setSettingsOpen] = useState(false);
	const [focus, setFocus] = useState<FocusRequest | null>(null);
	const editor = useRef<HTMLTextAreaElement>(null);
	useHeartbeat(!PUBLIC_DEMO);

	useEffect(() => {
		applyThemeChoice(theme);
		if (theme !== 'system') return;
		const query = window.matchMedia('(prefers-color-scheme: dark)');
		const follow = () => applyThemeChoice('system');
		query.addEventListener('change', follow);
		return () => query.removeEventListener('change', follow);
	}, [theme]);

	const show = (next: View) => { setView(next); window.scrollTo(0, 0); };
	const startReview = () => { show('review'); window.requestAnimationFrame(() => editor.current?.focus()); };
	const openSettings = () => { setSettingsOpen(true); show('review'); };
	const database = usableDatabase(state);
	const where = PUBLIC_DEMO ? 'Online demo' : state.target ? 'Remote: ' + targetName(state.target) : 'Local';

	return (
		<div className={'app' + (ambient ? '' : ' ambient-paused')}>
			<AmbientLight />
			<Sidebar views={VIEWS} view={view} onView={show} onNewReview={startReview}
				context={where + ' · ' + (database ? 'Database: ' + database : 'Static analysis only')} contextOn={database !== null}
				theme={theme} onTheme={choice => { setTheme(choice); saveThemeChoice(choice); }}
				ambient={ambient} onAmbient={() => { setAmbient(!ambient); writeStored(AMBIENT_KEY, ambient ? 'paused' : 'on'); }} />
			<main className="main">
				{PUBLIC_DEMO && (
					<p className="public-notice"><strong>Online demo:</strong> SQL is sent to the hosted server for static analysis.
						Do not paste confidential company scripts, credentials or production-sensitive information. Real database
						connections are available only in the downloaded application.</p>
				)}
				{!PUBLIC_DEMO && <UpdateNotice />}
				<div key={view} className="view-frame">
					{view === 'dashboard' && <DashboardView review={review} onStart={startReview} onOpenReview={() => show('review')}
						onOpenStatement={n => { show('review'); setFocus({ n, seq: Date.now() }); }} />}
					{view === 'review' && <ReviewView review={review} editorRef={editor} settingsOpen={settingsOpen}
						setSettingsOpen={setSettingsOpen} focus={focus} />}
					{view === 'performance' && <SnapshotView kind={PERFORMANCE} onOpenSettings={openSettings} />}
					{view === 'locks' && <SnapshotView kind={LOCKS} onOpenSettings={openSettings} />}
				</div>
			</main>
		</div>
	);
}
