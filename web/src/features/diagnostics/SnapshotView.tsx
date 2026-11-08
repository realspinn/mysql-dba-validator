// The shared frame of the two diagnostic views: context, one load/refresh action, and
// the snapshot or the reason there is none.
import { targetName } from '../../api/connector';
import { useConnectionContext } from '../connection/ConnectionContext';
import { usableDatabase } from '../connection/connectionState';
import type { LockSnapshot, PerfSnapshot } from '../../types/diagnostics';
import { LockResult } from './LockResult';
import { PerformanceResult } from './PerformanceResult';
import { stateText, type SnapshotKind } from './snapshotText';
import { useSnapshot } from './useSnapshot';

function DocsLink({ label, href }: { label: string; href: string }) {
	return <a className="docs-link" href={href} target="_blank" rel="noopener noreferrer">{label}</a>;
}

function Notice({ title, children, tone = '' }: { title: string; children: React.ReactNode; tone?: string }) {
	return <div className={'panel snapshot-state ' + tone}><strong>{title}</strong>{children}</div>;
}

export function SnapshotView({ kind, onOpenSettings }: { kind: SnapshotKind; onOpenSettings: () => void }) {
	const { state, actions } = useConnectionContext();
	const snapshot = useSnapshot(kind, state, actions);
	const database = usableDatabase(state) ?? '';
	const data = snapshot.result?.kind === 'data' ? snapshot.result.data : null;
	const items = data ? (data[kind.list] as unknown[]) : [];
	const hasData = data !== null && data.status === 'available' && items.length > 0;
	const observedAt = typeof data?.observed_at === 'string' ? data.observed_at : null;
	const observed = observedAt ? new Date(observedAt) : null;
	const settings = <button type="button" className="btn btn-secondary" onClick={onOpenSettings}>Open connection settings</button>;

	const live = snapshot.loading ? 'Reading Performance Schema.'
		: snapshot.result?.kind === 'failure' ? 'Request failed.'
			: data ? (hasData ? kind.loaded(items.length) : stateText(kind, String(data.status)).title + '.') : '';

	let body: React.ReactNode;
	if (snapshot.blocker && !snapshot.result) {
		body = <Notice title="Select a database first"><p>{snapshot.blocker}</p><div className="row">{settings}</div></Notice>;
	} else if (snapshot.loading && !snapshot.result) {
		body = <Notice title="Reading Performance Schema"><p>{kind.reading}</p><div className="progress" aria-hidden="true"><span /></div></Notice>;
	} else if (snapshot.result?.kind === 'failure') {
		body = <Notice title="Request failed" tone="notice"><p>{snapshot.result.message}</p></Notice>;
	} else if (hasData) {
		body = kind.list === 'families'
			? <PerformanceResult data={data as unknown as PerfSnapshot} />
			: <LockResult data={data as unknown as LockSnapshot} />;
	} else if (data) {
		const known = stateText(kind, String(data.status));
		body = (
			<Notice title={known.title} tone={data.status === kind.emptyStatus ? '' : 'notice'}>
				<p>{known.text(database)}</p>
				{(known.settings || known.docs) && <div className="row">{known.settings && settings}{known.docs && <DocsLink label={known.docs} href={kind.docs} />}</div>}
			</Notice>
		);
	} else {
		body = (
			<Notice title="No snapshot loaded">
				<p>{kind.intro(database)}</p>
				<ul className="facts">{kind.facts.map(([term, detail]) => <li key={term}><b>{term}.</b> {detail}</li>)}</ul>
				<div className="row"><DocsLink label="What this reads and why" href={kind.docs} /></div>
			</Notice>
		);
	}

	return (
		<section className="view snapshot" aria-labelledby={kind.route + '-title'}>
			<header className="page-head">
				<div>
					<h1 id={kind.route + '-title'}>{kind.title}</h1>
					<p>{kind.lede}</p>
				</div>
			</header>
			<div className="panel snapshot-bar">
				<dl className="context plain-context">
					<div><dt>Target</dt><dd>{state.target ? targetName(state.target) : 'Local'}</dd></div>
					<div><dt>Database</dt><dd className={database ? 'mono' : 'muted'}>{database || 'None selected'}</dd></div>
					<div><dt>Source</dt><dd><code>{kind.source}</code></dd></div>
					<div><dt>Observed at</dt><dd title={observedAt ? 'Validator clock, UTC: ' + observedAt : undefined}>
						{observed && !Number.isNaN(observed.getTime()) ? observed.toLocaleString() : 'Not loaded'}</dd></div>
				</dl>
				<button type="button" className={'btn ' + (snapshot.result ? 'btn-secondary' : 'btn-primary')}
					disabled={Boolean(snapshot.blocker) || snapshot.loading} aria-busy={snapshot.loading} onClick={() => void snapshot.load()}>
					{snapshot.result ? 'Refresh snapshot' : kind.loadLabel}
				</button>
			</div>
			<p className="visually-hidden" role="status">{live}</p>
			<div aria-busy={snapshot.loading}>{body}</div>
		</section>
	);
}
