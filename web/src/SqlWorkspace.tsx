import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import { ApiError, discoverDatabases, testConnection, validate, type Login, type Review } from './api';
import { Results } from './Results';

// Local Mode reaches MySQL on this computer only; the server enforces 127.0.0.1:3306.
const LOCAL_HOST = '127.0.0.1';
const LOCAL_PORT = 3306;

type Connection =
	| { state: 'none' }
	| { state: 'testing' }
	| { state: 'verified'; databases: string[] | null; complete: boolean }
	| { state: 'discovering' }
	| { state: 'failed'; message: string }
	| { state: 'discovery-failed'; message: string };

export interface WorkspaceState {
	sql: string;
	username: string;
	password: string;
	connection: Connection;
	database: string | null;
	review: { result: Review; sql: string; database: string | null; at: Date } | null;
	split: number;
}

const SPLIT_KEY = 'mdv-review-split';
function readSplit(): number {
	try {
		const value = Number(localStorage.getItem(SPLIT_KEY));
		if (value >= 32 && value <= 64) return value;
	} catch { /* default */ }
	return 46;
}

export const initialWorkspace = (): WorkspaceState => ({
	sql: '', username: '', password: '', connection: { state: 'none' }, database: null, review: null, split: readSplit(),
});

type Props = { state: WorkspaceState; setState: Dispatch<SetStateAction<WorkspaceState>> };
type Run = { status: 'idle' } | { status: 'running' } | { status: 'failed'; message: string };

export function SqlWorkspace({ state, setState }: Props) {
	const [run, setRun] = useState<Run>({ status: 'idle' });
	const [copied, setCopied] = useState(false);
	const abort = useRef<AbortController | null>(null);
	const patch = useCallback((change: Partial<WorkspaceState>) => setState(s => ({ ...s, ...change })), [setState]);
	useEffect(() => () => abort.current?.abort(), []);

	const login: Login | null = state.connection.state === 'verified' && state.username && state.password
		? { host: LOCAL_HOST, port: LOCAL_PORT, username: state.username, password: state.password } : null;
	const evidence = login !== null && state.database !== null;

	const runReview = async () => {
		const sql = state.sql;
		if (!sql.trim() || run.status === 'running') return;
		abort.current?.abort();
		const controller = new AbortController();
		abort.current = controller;
		setRun({ status: 'running' });
		const database = evidence ? state.database : null;
		try {
			const result = await validate(sql, evidence ? login : null, database, controller.signal);
			if (controller.signal.aborted) return;
			patch({ review: { result, sql, database, at: new Date() } });
			setRun({ status: 'idle' });
		} catch (error) {
			if (controller.signal.aborted) return;
			setRun({ status: 'failed', message: error instanceof ApiError ? error.message : 'The review did not complete.' });
		}
	};

	// Changing the login invalidates the verified connection and its database list.
	const editLogin = (change: Partial<WorkspaceState>) =>
		patch({ ...change, connection: { state: 'none' }, database: null });

	const connect = async () => {
		const attempt: Login = { host: LOCAL_HOST, port: LOCAL_PORT, username: state.username, password: state.password };
		patch({ connection: { state: 'testing' }, database: null });
		try {
			await testConnection(attempt);
		} catch (error) {
			patch({ connection: { state: 'failed', message: error instanceof ApiError ? error.message : 'Unable to connect.' } });
			return;
		}
		patch({ connection: { state: 'discovering' } });
		try {
			const found = await discoverDatabases(attempt);
			setState(s => (s.username === attempt.username && s.password === attempt.password
				? { ...s, connection: { state: 'verified', databases: found.databases, complete: found.complete } } : s));
		} catch (error) {
			patch({ connection: { state: 'discovery-failed', message: error instanceof ApiError ? error.message : 'Unable to list databases.' } });
		}
	};

	const copy = async () => {
		try { await navigator.clipboard.writeText(state.sql); setCopied(true); setTimeout(() => setCopied(false), 1400); } catch { /* not allowed */ }
	};

	const lines = state.sql.split('\n').length;
	const gutter = useRef<HTMLDivElement>(null);
	const stale = state.review !== null && (state.review.sql !== state.sql || state.review.database !== (evidence ? state.database : null));

	return (
		<section className="view workspace" aria-labelledby="reviewTitle">
			<header className="page-head">
				<div>
					<h1 id="reviewTitle">SQL review</h1>
					<p>Paste the SQL from a ticket. It is analysed, never executed.</p>
				</div>
				<ContextBar connection={state.connection} database={evidence ? state.database : null} />
			</header>

			<div className="split" style={{ ['--left' as string]: `${state.split}%` }}>
				<div className="pane pane-input">
					<div className="panel editor-panel">
						<div className="toolbar">
							<span className="toolbar-title">SQL</span>
							<span className="toolbar-meta">{lines} {lines === 1 ? 'line' : 'lines'} · {state.sql.length.toLocaleString()} characters</span>
							<span className="spacer" />
							<button type="button" className="btn btn-quiet btn-sm" onClick={copy} disabled={!state.sql}>{copied ? 'Copied' : 'Copy'}</button>
							<button type="button" className="btn btn-quiet btn-sm" onClick={() => patch({ sql: '' })} disabled={!state.sql}>Clear</button>
						</div>
						<div className="editor">
							<div className="gutter" ref={gutter} aria-hidden="true">
								{Array.from({ length: lines }, (_, i) => <span key={i}>{i + 1}</span>)}
							</div>
							<textarea aria-label="SQL to review" spellCheck={false} value={state.sql}
								placeholder={"UPDATE orders SET status = 'archived'\nWHERE created_at < '2025-01-01';"}
								onChange={e => patch({ sql: e.target.value })}
								onScroll={e => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop; }}
								onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void runReview(); } }} />
						</div>
						<div className="editor-actions">
							<button type="button" className="btn btn-primary" onClick={runReview}
								disabled={!state.sql.trim() || run.status === 'running'} aria-busy={run.status === 'running'}>
								{run.status === 'running' ? 'Reviewing…' : 'Review SQL'}
							</button>
							<span className="hint"><kbd>Ctrl</kbd><kbd>Enter</kbd></span>
							<span className="spacer" />
							<span className="mode-note">{evidence ? `With evidence from ${state.database}` : 'Static analysis'}</span>
						</div>
					</div>

					<ConnectionPanel state={state} editLogin={editLogin} connect={connect}
						selectDatabase={database => patch({ database })} />
				</div>

				<Separator value={state.split} onChange={split => {
					patch({ split });
					try { localStorage.setItem(SPLIT_KEY, String(split)); } catch { /* this page only */ }
				}} />

				<div className="pane pane-result">
					<Results run={run} review={state.review} stale={stale} onRetry={runReview} />
				</div>
			</div>
		</section>
	);
}

function ContextBar({ connection, database }: { connection: Connection; database: string | null }) {
	const login = connection.state === 'verified' ? 'Login verified'
		: connection.state === 'testing' || connection.state === 'discovering' ? 'Checking login'
			: connection.state === 'failed' ? 'Login failed' : 'No login';
	return (
		<dl className="context" aria-label="Review context">
			<div><dt>Mode</dt><dd>Local</dd></div>
			<div><dt>Server</dt><dd className="mono">{LOCAL_HOST}:{LOCAL_PORT}</dd></div>
			<div><dt>Login</dt><dd data-state={connection.state}>{login}</dd></div>
			<div><dt>Database</dt><dd className={database ? 'mono' : 'muted'}>{database ?? 'None'}</dd></div>
		</dl>
	);
}

type PanelProps = {
	state: WorkspaceState;
	editLogin: (change: Partial<WorkspaceState>) => void;
	connect: () => void;
	selectDatabase: (database: string | null) => void;
};

function ConnectionPanel({ state, editLogin, connect, selectDatabase }: PanelProps) {
	const c = state.connection;
	const busy = c.state === 'testing' || c.state === 'discovering';
	const databases = c.state === 'verified' ? c.databases ?? [] : [];
	const options: SelectProps.Option[] = databases.map(name => ({ label: name, value: name }));
	const statusType: SelectProps['statusType'] = c.state === 'discovering' ? 'loading'
		: c.state === 'discovery-failed' ? 'error' : 'finished';
	const status = c.state === 'testing' ? 'Testing the login…'
		: c.state === 'discovering' ? 'Login accepted. Listing databases…'
			: c.state === 'verified' ? `Login accepted by MySQL. ${databases.length} database${databases.length === 1 ? '' : 's'} visible${c.complete ? '' : ' (list incomplete)'}.`
				: c.state === 'failed' || c.state === 'discovery-failed' ? c.message
					: 'Optional. Adds execution-plan and table metadata evidence. The login is never saved.';

	return (
		<details className="panel connection" open={c.state !== 'none' || undefined}>
			<summary>
				<span className="summary-title">Database evidence</span>
				<span className="summary-meta">{c.state === 'verified' ? 'Connected' : 'Optional'}</span>
			</summary>
			<div className="connection-body">
				<div className="fields">
					<label className="field">
						<span>Server</span>
						<input value={`${LOCAL_HOST}:${LOCAL_PORT}`} readOnly aria-readonly="true" className="mono" />
					</label>
					<label className="field">
						<span>Username</span>
						<input value={state.username} autoComplete="off" spellCheck={false}
							onChange={e => editLogin({ username: e.target.value })} />
					</label>
					<label className="field">
						<span>Password</span>
						<input type="password" value={state.password} autoComplete="new-password"
							onChange={e => editLogin({ password: e.target.value })} />
					</label>
					<div className="field field-action">
						<button type="button" className="btn btn-secondary" onClick={connect}
							disabled={!state.username || !state.password || busy} aria-busy={busy}>
							{busy ? 'Connecting…' : c.state === 'verified' ? 'Reconnect' : 'Connect'}
						</button>
					</div>
				</div>
				<p className={'status-line' + (c.state === 'failed' || c.state === 'discovery-failed' ? ' error' : c.state === 'verified' ? ' ok' : '')}
					role="status">{status}</p>
				<div className="field">
					<span id="dbLabel">Database</span>
					<Select
						ariaLabelledby="dbLabel"
						selectedOption={state.database ? { label: state.database, value: state.database } : null}
						onChange={e => selectDatabase(e.detail.selectedOption.value ?? null)}
						options={options}
						disabled={c.state !== 'verified' && c.state !== 'discovering' && c.state !== 'discovery-failed'}
						placeholder={c.state === 'verified' ? 'Choose a database' : 'Connect to list databases'}
						filteringType={options.length > 7 ? 'auto' : 'none'}
						filteringPlaceholder="Filter databases"
						filteringAriaLabel="Filter databases"
						statusType={statusType}
						loadingText="Listing databases"
						errorText={c.state === 'discovery-failed' ? c.message : undefined}
						empty="No databases are visible to this login"
						expandToViewport
					/>
				</div>
			</div>
		</details>
	);
}

function Separator({ value, onChange }: { value: number; onChange: (value: number) => void }) {
	const clamp = (n: number) => Math.min(64, Math.max(32, Math.round(n)));
	const start = (event: React.PointerEvent<HTMLDivElement>) => {
		const container = event.currentTarget.parentElement!;
		const box = container.getBoundingClientRect();
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
