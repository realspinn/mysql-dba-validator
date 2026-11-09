// Observed performance and lock diagnostics: what each view reads, its wording for each
// stable status the server reports, and the value formats. Pure: no DOM.

export const PERF_DOCS = 'https://github.com/realspinn/mysql-dba-validator/blob/main/docs/historical-schema-workload.md';
export const LOCK_DOCS = 'https://github.com/realspinn/mysql-dba-validator/blob/main/docs/innodb-lock-waits.md';

// Milliseconds from the API (rounded there to 0.001 ms, so microseconds are exact).
// Null means MySQL recorded no time: never shown as 0.
export function formatDuration(ms: number | null | undefined): string {
	if (ms == null) return 'Not recorded';
	if (ms >= 1000) return (ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' s';
	if (ms >= 1) return ms.toLocaleString(undefined, { maximumFractionDigits: ms >= 100 ? 0 : 1 }) + ' ms';
	const us = Math.round(ms * 1000);
	return us >= 1 ? us + ' µs' : 'Under 1 µs';
}

export function exactDuration(ms: number | null | undefined): string {
	if (ms == null) return 'Not recorded';
	if (ms === 0) return 'Under 0.001 ms'; // recorded, but below the API's 0.001 ms resolution
	return ms.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 }) + ' ms';
}

export const formatCount = (value: unknown) => (typeof value === 'number' ? value.toLocaleString() : 'n/a');

// FIRST_SEEN / LAST_SEEN as MySQL returned them (server time, no zone).
export const serverTime = (value: unknown) =>
	typeof value === 'string' ? value.replace('T', ' ').replace(/\.\d+$/, '') : 'n/a';

export function formatSeconds(value: unknown): string {
	if (typeof value !== 'number') return 'Unknown';
	if (value < 1) return 'Under 1 s';
	if (value < 60) return value + ' s';
	return Math.floor(value / 60) + ' min ' + (value % 60) + ' s';
}

export interface StateText { title: string; text: (db: string) => string; docs?: string; settings?: boolean }

const PERF_STATES: Record<string, StateText> = {
	no_observations: {
		title: 'No observations recorded',
		text: db => 'MySQL has no statement families recorded for ' + db + ' since the digest table was last reset or the server restarted. Run the workload you want to inspect, then refresh the snapshot.',
	},
	insufficient_privileges: {
		title: 'Unable to read Performance Schema',
		text: () => 'Your MySQL account cannot read the statement digest summary for this database. A DBA can grant SELECT on the columns this view reads. That privilege is server-wide: the account can then read normalized statements from every schema, so granting it is a DBA decision. The validator never requests or changes privileges.',
		docs: 'Required privileges',
	},
	performance_schema_disabled: {
		title: 'Performance Schema is disabled',
		text: () => 'This server runs with performance_schema off, so MySQL records no statement families. It can only be enabled at server startup. The validator never changes server settings.',
	},
	digest_consumer_disabled: {
		title: 'Statement digests are not being collected',
		text: () => 'The statements_digest or global_instrumentation consumer is disabled in performance_schema.setup_consumers, so MySQL is not aggregating statement families. The validator never changes instrumentation.',
	},
	collection_failed: {
		title: 'Collection did not complete',
		text: () => 'The Performance Schema read did not complete, for example because the connection was lost. Nothing more was sent on that connection. Refresh the snapshot to try again.',
	},
	unexpected_response: {
		title: 'Unexpected server response',
		text: () => 'MySQL returned values that do not match the documented digest summary, so no partial result is shown.',
	},
	auth_failed: {
		title: 'MySQL login failed',
		text: () => 'Check the username and password in the SQL review connection settings, then refresh the snapshot.',
		settings: true,
	},
	unreachable: {
		title: 'Database connection unavailable',
		text: () => 'MySQL could not be reached. Check that the server is running and reachable, then refresh the snapshot.',
	},
	database_not_found: {
		title: 'Database not found',
		text: db => db + ' does not exist or your account cannot see it. Discover databases again in the connection settings.',
		settings: true,
	},
};

const LOCK_STATES: Record<string, StateText> = {
	collection_failed: PERF_STATES.collection_failed!,
	unexpected_response: {
		title: 'Unexpected server response',
		text: () => 'MySQL returned values that do not match the documented tables, so no partial result is shown.',
	},
	auth_failed: PERF_STATES.auth_failed!,
	unreachable: PERF_STATES.unreachable!,
	database_not_found: PERF_STATES.database_not_found!,
	no_lock_waits: {
		title: 'No lock waits found',
		text: db => 'At the time shown, no InnoDB lock wait was found on tables in ' + db + '. This covers row and table lock waits at that moment only: metadata locks, other databases and waits that began or ended a moment earlier are not shown.',
	},
	insufficient_privileges: {
		title: 'Unable to read lock information',
		text: () => 'Your MySQL account cannot read the InnoDB lock tables in Performance Schema. A DBA can grant SELECT on the lock columns this view reads. Those privileges show lock waits for every schema, so granting them is a DBA decision. The validator never requests or changes privileges.',
		docs: 'Required privileges',
	},
	performance_schema_disabled: {
		title: 'Performance Schema is disabled',
		text: () => 'This server runs with performance_schema off, so lock waits cannot be read from it. It can only be enabled at server startup. The validator never changes server settings.',
	},
};

export interface SnapshotKind {
	route: 'observed-performance' | 'lock-waits';
	list: 'families' | 'waits';
	docs: string;
	title: string;
	lede: string;
	source: string;
	subject: string;
	loadLabel: string;
	emptyStatus: string;
	reading: string;
	intro: (db: string) => string;
	facts: [string, string][];
	states: Record<string, StateText>;
	loaded: (count: number) => string;
}

export const PERFORMANCE: SnapshotKind = {
	route: 'observed-performance', list: 'families', docs: PERF_DOCS,
	title: 'Historical workload',
	lede: 'Statement families MySQL has already recorded for the selected database, read from Performance Schema when you ask. Nothing is executed and no SQL review changes.',
	source: 'events_statements_summary_by_digest',
	subject: 'Historical workload', loadLabel: 'Load historical workload', emptyStatus: 'no_observations',
	reading: 'Four fixed queries; nothing you submitted is sent.',
	intro: db => 'Load the statement families MySQL has recorded for ' + db + ': executions, recorded time and rows examined, ranked by total recorded time.',
	facts: [
		['Historical', 'Counts and recorded time accumulate from when MySQL first saw each statement family. They describe past workload, not the SQL you are reviewing.'],
		['Fixed queries', 'The validator sends four fixed Performance Schema queries of its own. Submitted SQL is never executed and no risk assessment changes.'],
		['Permissions', 'Your MySQL account needs SELECT on the digest summary columns. That privilege is server-wide.'],
	],
	states: PERF_STATES,
	loaded: count => 'Snapshot loaded: ' + count + ' statement families.',
};

export const LOCKS: SnapshotKind = {
	route: 'lock-waits', list: 'waits', docs: LOCK_DOCS,
	title: 'InnoDB lock waits',
	lede: 'Who is waiting, who is blocking and where, in the selected database, read from Performance Schema when you ask. Nothing is killed, committed or rolled back.',
	source: 'data_lock_waits',
	subject: 'Lock diagnostics', loadLabel: 'Read lock waits', emptyStatus: 'no_lock_waits',
	reading: 'Three fixed queries; nothing you submitted is sent and no session is touched.',
	intro: db => 'Read the InnoDB lock waits on tables in ' + db + ' right now: which session waits, which session blocks it, and on which table and index.',
	facts: [
		['Snapshot', 'Locks change within seconds. The result describes one moment; refresh it before acting on it.'],
		['Fixed queries', 'The validator sends three fixed Performance Schema queries of its own. It never kills, commits or rolls back anything, and never reads row values or SQL text.'],
		['Permissions', 'Your MySQL account needs SELECT on lock columns in Performance Schema. Those privileges show lock waits for every schema.'],
	],
	states: LOCK_STATES,
	loaded: count => 'Snapshot loaded: ' + count + ' lock waits.',
};

export const stateText = (kind: SnapshotKind, status: string): StateText =>
	kind.states[status] ?? kind.states.unexpected_response!;
