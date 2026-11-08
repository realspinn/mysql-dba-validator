// SQL review: the SQL, the optional database evidence settings, and the result.
import { useState } from 'react';
import { targetName } from '../../api/connector';
import { LOCAL_HOST, LOCAL_PORT, PUBLIC_DEMO } from '../../shared/environment';
import { readStored, writeStored } from '../../shared/storage';
import { ConnectionPanel } from '../connection/ConnectionPanel';
import { useConnectionContext } from '../connection/ConnectionContext';
import { usableDatabase, type ConnectionState } from '../connection/connectionState';
import { Results } from './Results';
import type { FocusRequest } from './StatementList';
import { SqlEditor } from './SqlEditor';
import { reviewContext, type ReviewStore } from './useReview';

const SPLIT_KEY = 'mdv-review-split';
const readSplit = () => {
	const value = Number(readStored(SPLIT_KEY));
	return value >= 32 && value <= 64 ? value : 46;
};

export function ReviewView({ review, editorRef, settingsOpen, setSettingsOpen, focus }: {
	review: ReviewStore;
	focus: FocusRequest | null;
	editorRef: React.Ref<HTMLTextAreaElement>;
	settingsOpen: boolean;
	setSettingsOpen: (open: boolean) => void;
}) {
	const { state } = useConnectionContext();
	const [split, setSplit] = useState(readSplit);
	const run = () => void review.start(review.sql);
	const database = usableDatabase(state);
	const stale = review.reviewed !== null
		&& (review.reviewed.sql !== review.sql || review.reviewed.context !== reviewContext(state));
	const note = PUBLIC_DEMO ? 'Static analysis · online demo'
		: state.target ? 'Through the connector: ' + targetName(state.target) + (database ? ' · ' + database : '')
			: database ? 'With evidence from ' + database : state.verified ? 'Connected · no database chosen' : 'Static analysis';

	return (
		<section className="view workspace" aria-labelledby="reviewTitle">
			<header className="page-head">
				<div>
					<h1 id="reviewTitle">SQL review</h1>
					<p>{PUBLIC_DEMO ? 'Paste SQL and review it. The online demo analyses the SQL text only.'
						: 'Paste the SQL from a ticket. It is analysed, never executed. A database connection is optional.'}</p>
				</div>
				<ContextBar state={state} />
			</header>

			<div className="split" style={{ ['--left' as string]: `${split}%` }}>
				<div className="pane pane-input">
					<SqlEditor value={review.sql} onChange={review.setSql} onRun={run} running={review.run.status === 'running'}
						note={note} textareaRef={editorRef} />
					{PUBLIC_DEMO
						? <p className="panel demo-note">Paste up to 16 MySQL statements, up to 64 KiB. Database evidence is
							available in the downloaded application.</p>
						: <ConnectionPanel open={settingsOpen} onToggle={setSettingsOpen} />}
				</div>
				<Separator value={split} onChange={value => { setSplit(value); writeStored(SPLIT_KEY, String(value)); }} />
				<div className="pane pane-result">
					<Results run={review.run} reviewed={review.reviewed} stale={stale} onRetry={run} focus={focus} />
				</div>
			</div>
		</section>
	);
}

export function ContextBar({ state }: { state: ConnectionState }) {
	const database = usableDatabase(state);
	const login = state.verified ? 'Login verified' : state.busy ? 'Checking login'
		: state.connectionStatus.kind === 'error' ? 'Login failed' : 'No login';
	if (PUBLIC_DEMO) {
		return (
			<dl className="context" aria-label="Review context">
				<div><dt>Mode</dt><dd>Online demo</dd></div>
				<div><dt>Evidence</dt><dd className="muted">Static analysis only</dd></div>
			</dl>
		);
	}
	return (
		<dl className="context" aria-label="Review context">
			<div><dt>Mode</dt><dd>{state.target ? 'Company' : 'Local'}</dd></div>
			<div><dt>Server</dt><dd className="mono">{state.target ? targetName(state.target) : `${LOCAL_HOST}:${LOCAL_PORT}`}</dd></div>
			<div><dt>Login</dt><dd data-state={state.verified ? 'verified' : state.connectionStatus.kind === 'error' ? 'failed' : ''}>{login}</dd></div>
			<div><dt>Database</dt><dd className={database ? 'mono' : 'muted'}>{database ?? 'None'}</dd></div>
		</dl>
	);
}

function Separator({ value, onChange }: { value: number; onChange: (value: number) => void }) {
	const clamp = (n: number) => Math.min(64, Math.max(32, Math.round(n)));
	const start = (event: React.PointerEvent<HTMLDivElement>) => {
		const box = event.currentTarget.parentElement!.getBoundingClientRect();
		event.currentTarget.setPointerCapture(event.pointerId);
		const move = (e: PointerEvent) => onChange(clamp(((e.clientX - box.left) / box.width) * 100));
		const end = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', end); };
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', end);
	};
	return (
		<div className="separator" role="separator" aria-orientation="vertical" aria-label="Resize editor and result"
			aria-valuemin={32} aria-valuemax={64} aria-valuenow={value} tabIndex={0} onPointerDown={start}
			onKeyDown={e => {
				if (e.key === 'ArrowLeft') onChange(clamp(value - 2));
				else if (e.key === 'ArrowRight') onChange(clamp(value + 2));
			}}><span /></div>
	);
}
