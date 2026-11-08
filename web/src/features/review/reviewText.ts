// What a review says, derived only from fields the server returned. It never calls SQL
// safe, never states a row count and never implies a plan that was not collected.
// Pure functions with no DOM, so the wording is tested directly (reviewText.test.ts).
import type { EvidenceState, EvidenceSummary, Factor, Optimization, ReviewResponse, Statement } from '../../types/review';

// The online demo has no database: a few lines say so instead of asking for a login.
let demo = false;
export function setDemoWording(value: boolean): void { demo = value; }
const noDatabase = () => (demo ? 'the online demo does not connect to databases.' : 'no database login and database.');

// The CSS tone for a risk level (styles/review.css).
export const riskClass = (level: string | null | undefined) => 'risk-' + (level ?? 'unknown').toLowerCase();

export const text = (value: unknown): string => (value == null || value === '' ? 'n/a' : String(value));
const fixed = (messages: Readonly<Record<string, string>>, code: string | null | undefined): string | null =>
	code != null && Object.hasOwn(messages, code) ? messages[code] ?? null : null;

export function analysisModeLabel(mode: string | undefined): string {
	return mode === 'DATABASE_EVIDENCE' ? 'Database evidence'
		: mode === 'STATIC_DATABASE_METADATA' ? 'Static + database metadata'
			: mode === 'STATIC_DATABASE_UNAVAILABLE' ? 'Static + database evidence unavailable'
				: 'Static analysis';
}

// The statement's evidence state (server source of truth). Results without one are static only.
export function evidenceOf(s: Statement): EvidenceState {
	return s.evidence ?? {
		connection: { state: 'not_attempted', reason: 'not_configured' },
		plan: { state: 'not_attempted', reason: null },
		metadata: { state: 'not_attempted', reason: null, tables: [] },
		overall: 'static_only',
	};
}

export function metadataStatusLabel(metadata: EvidenceState['metadata']): string {
	return metadata.state === 'collected' ? 'Collected' : metadata.state === 'not_applicable' ? 'Not applicable' : 'Not collected';
}

// Why evidence is missing, from the stable status the server reports.
const EVIDENCE_STATUS_MESSAGES: Record<string, string> = {
	not_configured: 'Enter your database login and select a database, then validate again.',
	unreachable: 'The database could not be reached. Check that MySQL is running and reachable.',
	auth_failed: 'The database rejected your login. Check the username and password.',
	database_not_found: 'The selected database was not found. Discover databases again.',
	insufficient_privileges: 'Your account lacks a privilege this evidence needs. EXPLAIN requires the same privileges as the statement itself.',
	explain_failed: 'MySQL could not produce an EXPLAIN plan for this statement.',
	unsupported_statement_type: 'No execution plan is collected for this statement type.',
	collection_failed: 'Evidence collection failed. The static analysis is still complete.',
	plan_not_collected_read_only: 'Execution plan not requested: the validator does not request plans for UPDATE or DELETE, because evidence connections are read-only and it never asks for write access.',
	plan_not_eligible: 'No execution plan was requested: this statement is not eligible for planning (for example SELECT ... INTO, or a function call that is not on the reviewed list of MySQL built-ins).',
	explain_format_unsupported: 'No execution plan is shown: MySQL returned it in a format the validator does not read. The server explain_format setting must be TRADITIONAL.',
};
const DEMO_NOT_CONFIGURED = 'Database evidence is available only in the downloaded application.';
export const evidenceStatusMessage = (code: string | null | undefined): string | null =>
	demo && code === 'not_configured' ? DEMO_NOT_CONFIGURED : fixed(EVIDENCE_STATUS_MESSAGES, code);

// Why table metadata was not collected, from the stable code the server reports.
const METADATA_REASON_MESSAGES: Record<string, string> = {
	timeout: 'Database metadata collection timed out.',
	permission_denied: 'Your account was denied access to the table metadata.',
	budget_exceeded: 'Database metadata collection exceeded its size limit.',
	invalid_reference: 'A table reference in this statement is not supported for metadata.',
	unavailable: 'Database metadata was unavailable.',
	no_database_selected: 'No database was selected, so table metadata was not collected.',
};

// One headline per overall evidence state; "collected" is named per evidence type:
// that a table exists says nothing about how many rows a statement matches.
const EVIDENCE_HEADLINES: Record<string, string> = {
	static_only: 'Assessment based on SQL structure only.',
	unavailable: 'Database evidence could not be collected.',
	partial: 'Partial database evidence: some evidence was collected and some could not be.',
	not_applicable: 'Connected to the database; no database evidence applies to this statement.',
};
const COLLECTED_HEADLINES = {
	both: 'Collected: table metadata and an execution plan (optimizer estimates).',
	metadata: 'Collected: table metadata only.',
	plan: 'Collected: an execution plan only (optimizer estimates).',
};

export function evidenceHeadline(state: EvidenceState): string {
	if (state.overall === 'collected') {
		const metadata = state.metadata.state === 'collected';
		const plan = state.plan.state === 'collected';
		return COLLECTED_HEADLINES[metadata && plan ? 'both' : plan ? 'plan' : 'metadata'];
	}
	return fixed(EVIDENCE_HEADLINES, state.overall) ?? EVIDENCE_HEADLINES.unavailable!;
}

// What is established about the rows an UPDATE or DELETE matches directly. Never a row
// count. Indirect effects (triggers, foreign-key actions) are never inspected.
const INDIRECT_EFFECTS_NOTE = ' Triggers and foreign-key actions (such as ON DELETE CASCADE) were not checked.';

export function scopeLine(s: Statement): string | null {
	const scope = s.scope;
	if (!scope) return null;
	const indirect = scope.indirect_effects === 'not_checked' ? INDIRECT_EFFECTS_NOTE : '';
	if (scope.basis === 'all_rows')
		return 'Row scope: every row of ' + text(scope.table) + ' (from the SQL: no WHERE clause, or one that is always true).' + indirect;
	if (scope.basis === 'at_most_one_row')
		return 'Row scope: at most one row of ' + text(scope.table) + ' matches directly, by unique key '
			+ text(scope.index) + ' (confirmed from table metadata). The row may not exist.' + indirect;
	if (scope.basis === 'not_established') {
		const connected = evidenceOf(s).connection.state === 'connected';
		return 'Row scope: not established. A WHERE clause can match zero, one, some or all rows'
			+ (connected ? '; the database evidence did not establish how many.' : '; the SQL alone does not say how many.') + indirect;
	}
	return null;
}

export function planLine(plan: EvidenceState['plan']): string | null {
	if (plan.state === 'collected' && plan.interpreted === false)
		return 'Multi-step execution plan (' + text(plan.rows) + ' rows) collected: shown for reference, not summarised or used for scoring.';
	if (plan.state === 'collected') return 'Execution-plan evidence was collected (optimizer estimates, not rows changed).';
	if (plan.state === 'not_collected_read_only') return EVIDENCE_STATUS_MESSAGES.plan_not_collected_read_only!;
	if (plan.state === 'not_eligible') return EVIDENCE_STATUS_MESSAGES.plan_not_eligible!;
	if (plan.state === 'not_supported') return EVIDENCE_STATUS_MESSAGES.unsupported_statement_type!;
	if (plan.state === 'failed') return 'Execution-plan evidence could not be collected. ' + (evidenceStatusMessage(plan.reason) ?? '');
	return null;
}

export function metadataLines(metadata: EvidenceState['metadata']): string[] {
	if (metadata.state === 'collected') {
		return ['Database metadata was collected.', ...(metadata.tables ?? []).map(table => text(table.name) + ': '
			+ (table.found === true ? 'table found' : table.found === false ? 'not found in the selected database'
				: 'existence could not be determined'))];
	}
	if (metadata.state === 'failed')
		return ['Database metadata could not be collected. ' + (fixed(METADATA_REASON_MESSAGES, metadata.reason) ?? '')];
	if (metadata.state === 'not_applicable') return ['No table metadata applies to this statement.'];
	const reason = fixed(METADATA_REASON_MESSAGES, metadata.reason);
	return reason ? [reason] : [];
}

// The evidence layer of one statement: headline, then why there is no connection, or
// what the connection produced.
export interface EvidenceView {
	headline: string;
	reason: string | null;
	metadata: string[];
	plan: string | null;
	planFields: [string, string][] | null;
	planExtra: string | null;
}

export function evidenceView(s: Statement): EvidenceView {
	const state = evidenceOf(s);
	const evidence = s.database_evidence ?? {};
	const connected = state.connection.state === 'connected';
	// Plan fields only for a collected one-row plan: never a grid of blanks, and never
	// the first row of a multi-step plan presented as the whole plan.
	const onePlan = state.plan.state === 'collected' && state.plan.interpreted !== false;
	return {
		headline: evidenceHeadline(state),
		reason: connected ? null : evidenceStatusMessage(state.connection.reason),
		metadata: connected ? metadataLines(state.metadata) : [],
		plan: connected ? planLine(state.plan) : null,
		planFields: onePlan ? [
			['Access type', text(evidence.access_type)],
			['Estimated rows examined', evidence.estimated_rows == null ? 'n/a' : evidence.estimated_rows + ' (estimate)'],
			['Possible keys', evidence.possible_keys && evidence.possible_keys.length ? evidence.possible_keys.join(', ') : 'n/a'],
			['Selected key', text(evidence.key)],
			['Key length', text(evidence.key_length)],
			['Full table scan', evidence.full_table_scan == null ? 'n/a' : evidence.full_table_scan ? 'Yes' : 'No'],
		] : null,
		planExtra: onePlan && evidence.extra ? 'Additional plan detail: ' + evidence.extra : null,
	};
}

export const factorLabel = (f: Factor) => text(f.label || f.text || f.code);
export const pointsText = (points: number) => (points > 0 ? '+' : '') + points + ' points';

const formatRows = (value: unknown) => (typeof value === 'number' ? value.toLocaleString('en-US') : 'n/a');
const rowsWord = (value: unknown) => (value === 1 ? ' row' : ' rows');

// Plan and metadata factors in a DBA's words. Estimates stay labelled as estimates.
export function factorText(s: Statement, factor: Factor): string {
	const label = factorLabel(factor);
	const rows = s.database_evidence?.estimated_rows;
	if (label === 'EXPLAIN indicates a full table scan') return 'Full table scan (EXPLAIN access type ALL)';
	if (label === 'EXPLAIN estimated rows are high') {
		return typeof rows === 'number'
			? 'Optimizer estimate of ' + rows.toLocaleString('en-US') + rowsWord(rows) + ' examined (estimate)'
			: 'High optimizer row estimate (estimate)';
	}
	if (String(factor.code || '').startsWith('M2_')) {
		const column = s.filter_index?.column;
		return 'Full scan, and no index starts with the filter column' + (column ? ' ' + column : '') + ' (table metadata)';
	}
	return label;
}

// The plan in one line: access path and the optimizer's row estimate, never a count.
export function scanLine(s: Statement): string {
	const plan = evidenceOf(s).plan;
	const evidence = s.database_evidence ?? {};
	if (plan.state === 'collected' && plan.interpreted === false)
		return 'Multi-step plan (' + text(plan.rows) + ' rows): shown under Database evidence, not summarised.';
	if (plan.state === 'collected') {
		const path = evidence.full_table_scan
			? 'Full table scan (access type ALL)'
			: 'Access type ' + text(evidence.access_type) + (evidence.key ? ' using key ' + evidence.key : ', no key used');
		const extra = typeof evidence.extra === 'string' ? evidence.extra : '';
		const steps = ['Using filesort', 'Using temporary'].filter(step => extra.includes(step));
		const sorted = !steps.length ? ''
			: ' The plan shows ' + steps.join(' and ') + (s.has_limit ? ': every matching row is read and sorted before LIMIT applies.' : '.');
		return path + '; optimizer estimate: ' + formatRows(evidence.estimated_rows) + rowsWord(evidence.estimated_rows) + ' examined.' + sorted;
	}
	if (plan.state === 'not_collected_read_only') return 'Not collected: plans are not requested for UPDATE or DELETE.';
	if (plan.state === 'not_eligible') return 'Not collected: this statement is not eligible for a plan.';
	if (plan.state === 'not_supported') return 'Not applicable to this statement type.';
	if (plan.state === 'failed') return 'Collection failed. ' + (evidenceStatusMessage(plan.reason) ?? '');
	return 'Not collected: ' + noDatabase();
}

// The filter and the table's indexes, from table metadata (server: filter_index).
export function filterLine(s: Statement): string {
	const filter = s.filter_index ?? {};
	const on = filter.table ? ' on ' + filter.table : '';
	if (filter.state === 'no_filter') return 'No WHERE filter.';
	if (filter.state === 'not_assessed') return 'Not assessed: only a single-column comparison with a value, on one table, is checked.';
	if (filter.state === 'matching_index') return 'An index' + on + ' starts with ' + text(filter.column) + ' (table metadata). The plan shows whether MySQL uses it.';
	if (filter.state === 'no_matching_index') return 'No index' + on + ' starts with ' + text(filter.column) + ' (table metadata).';
	if (filter.state === 'not_checked')
		return 'Not checked: ' + (evidenceOf(s).connection.state === 'connected' ? 'complete table and index metadata were not available.' : noDatabase());
	return 'Not applicable to this statement type.';
}

// The plan-and-index layer of one statement.
export function metadataSummary(s: Statement): { status: string; adjustment: string; m1: Factor[]; m2: Factor[] } {
	const factors = s.metadata_factors ?? [];
	const metadata = evidenceOf(s).metadata;
	const adjustment = s.metadata_score_adjustment || 0;
	return {
		status: 'Metadata: ' + metadataStatusLabel(metadata),
		adjustment: metadata.state === 'collected' ? 'Risk adjustment: ' + (adjustment > 0 ? '+' : '') + adjustment + ' points'
			: 'Metadata-based scoring was not applied.',
		m1: factors.filter(f => String(f.code || '').startsWith('M1_')),
		m2: factors.filter(f => String(f.code || '').startsWith('M2_')),
	};
}

// Optimization advice: separate from the risk result. Every item is an observation
// from the SQL; nothing here is measured or claims a speed-up.
export const OPTIMIZATION_RULES = 'The rules checked are: SELECT * over tables, tables joined without a join condition, a LIKE pattern that starts with a wildcard, a filtered column inside a function or calculation, a HAVING condition on a grouping column, ORDER BY RAND(), and a UNION that removes duplicates. The join, HAVING and UNION checks look at the outer query only, not inside subqueries, derived tables or CTEs. This is not a full performance review.';

export function optimizationSummary(o: Optimization): string {
	const count = (o.recommendations ?? []).length;
	const summary = o.status === 'advised' ? 'Optimization advice: ' + count + ' observation' + (count === 1 ? '' : 's')
		: o.status === 'none_found' ? 'Optimization advice: no observations from the rules checked'
			: 'Optimization advice: not checked';
	return summary + ' (advisory; does not change the risk score)';
}

const PLAN_COUNT_LABELS: Record<string, string> = {
	collected: 'collected',
	not_collected_read_only: 'not collected (read-only connection)',
	not_eligible: 'not eligible',
	not_supported: 'not applicable',
	failed: 'could not be collected',
};
const METADATA_COUNT_LABELS: Record<string, string> = {
	collected: 'collected',
	failed: 'could not be collected',
	not_applicable: 'not applicable',
	not_attempted: 'not collected',
};

const countsText = (counts: Record<string, number> | undefined, labels: Record<string, string>) =>
	Object.keys(labels).filter(key => counts && counts[key]).map(key => counts![key] + ' ' + labels[key]).join(', ');

// What the batch's database evidence actually was; the analysis mode label alone never
// implies that all evidence was collected.
export function evidenceSummaryText(summary: EvidenceSummary | undefined): string {
	if (!summary || summary.connection.state === 'not_attempted') return 'Assessment based on SQL structure only.';
	if (summary.connection.state === 'failed') return 'Database evidence could not be collected.';
	const parts: string[] = [];
	const plan = countsText(summary.plan, PLAN_COUNT_LABELS);
	const metadata = countsText(summary.metadata, METADATA_COUNT_LABELS);
	if (plan) parts.push('Execution plan: ' + plan);
	if (metadata) parts.push('Metadata: ' + metadata);
	if (summary.partial) parts.push('Partial evidence');
	return parts.join(' · ') || 'Connected; no database evidence applies.';
}

const RISK_ORDER: Record<string, number> = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 };
const riskRank = (s: Statement) => RISK_ORDER[s.risk_level ?? ''] ?? 0;

// Why a statement was not analysed (analysis_status). An unsupported statement is
// valid MySQL; an unreadable one may or may not be valid.
const NOT_ANALYSED_LABELS: Record<string, string> = {
	invalid: 'Not valid MySQL: ',
	unsupported: 'Not analysed (not supported): ',
	unreadable: 'Could not be read: ',
};
const NOT_ANALYSED_REASONS: Record<string, string> = {
	invalid: 'not valid MySQL',
	unsupported: 'valid MySQL, but this statement type is not supported',
	unreadable: 'the validator could not read it',
};

export const allFactors = (s: Statement): Factor[] => [...(s.factors ?? []), ...(s.evidence_factors ?? []), ...(s.metadata_factors ?? [])];

// The statement's largest scoring factor, or why it was not analysed.
export function keyFinding(s: Statement): string {
	if (s.parse_error) return (fixed(NOT_ANALYSED_LABELS, s.analysis_status) ?? 'Not analysed: ') + s.parse_error;
	const top = allFactors(s).filter(f => (f.points ?? 0) > 0).sort((a, b) => (b.points ?? 0) - (a.points ?? 0))[0];
	return top ? factorText(s, top) + ' (+' + top.points + ')' : 'No rule raised the score.';
}

export const checklistText = (item: string | { text?: string; name?: string }) =>
	typeof item === 'string' ? item : (item.text || item.name || '');

export interface Finding { level: string; points: number; n: number; text: string }
export interface Line { text: string }
export type EvidenceRow = [term: string, detail: string, coverage: 'done' | 'partial' | 'none' | 'info'];

export interface ReviewModel {
	statements: Statement[];
	limits: Line[];
	findings: Finding[];
	actions: Line[];
	evidenceRows: EvidenceRow[];
	unknowns: EvidenceRow[];
	top: { s: Statement; n: number } | null;
}

export function reviewModel(data: ReviewResponse): ReviewModel {
	const statements = data.statements ?? [];
	const ranked = statements.map((s, index) => ({ s, n: index + 1 }))
		.sort((a, b) => riskRank(b.s) - riskRank(a.s) || (b.s.score || 0) - (a.s.score || 0));
	const summary = data.evidence_summary;
	const connection = summary ? summary.connection.state : 'not_attempted';
	const writes = statements.filter(s => s.statement_type === 'UPDATE' || s.statement_type === 'DELETE');

	const limits: Line[] = [];
	if (connection === 'not_attempted') limits.push({ text: 'No database evidence: based on the SQL text only.' });
	if (connection === 'failed') limits.push({ text: 'The database connection failed: based on the SQL text only.' });
	ranked.forEach(({ s, n }) => {
		const label = 'Statement ' + n + ': ';
		if (s.parse_error) limits.push({ text: label + 'not analysed: ' + (fixed(NOT_ANALYSED_REASONS, s.analysis_status) ?? 'it could not be analysed') + '.' });
		else if (s.analysis_text_differs) limits.push({ text: label + 'the SQL could only be read partly.' });
		const state = evidenceOf(s);
		if (state.metadata.state === 'collected') {
			(state.metadata.tables ?? []).forEach(table => {
				if (table.found === false) limits.push({ text: label + text(table.name) + ' was not found in the selected database.' });
				else if (table.found !== true) limits.push({ text: label + 'whether ' + text(table.name) + ' exists could not be determined.' });
			});
		}
		if (state.plan.state === 'failed') limits.push({ text: label + 'the execution plan could not be collected.' });
		if (state.metadata.state === 'failed') limits.push({ text: label + 'table metadata could not be collected.' });
		if (s.scope && s.scope.basis === 'not_established') limits.push({ text: label + 'the rows it can change are not established.' });
	});

	const findings: Finding[] = [];
	ranked.forEach(({ s, n }) => allFactors(s).filter(f => (f.points ?? 0) > 0).forEach(f => findings.push({
		level: text(s.risk_level), points: f.points ?? 0, n,
		text: 'Statement ' + n + ': ' + factorText(s, f) + ' (+' + f.points + ')',
	})));
	// Highest severity first, then the largest score contribution.
	findings.sort((a, b) => (RISK_ORDER[b.level] ?? 0) - (RISK_ORDER[a.level] ?? 0) || b.points - a.points);

	const actions: Line[] = [];
	ranked.forEach(({ s }) => (s.checklist ?? []).forEach(item => {
		const line = checklistText(item);
		if (line && !actions.some(a => a.text === line)) actions.push({ text: line });
	}));

	const counts = summary?.metadata ?? {};
	const planCounts = summary?.plan ?? {};
	const metadataParts: string[] = [];
	if (counts.collected) metadataParts.push('collected for ' + counts.collected + ' statement(s)');
	if (counts.failed) metadataParts.push('collection failed for ' + counts.failed + ' statement(s)');
	if (counts.not_attempted) metadataParts.push('not collected for ' + counts.not_attempted + ' statement(s) (no database selected)');
	const capital = (value: string) => value.replace(/^./, c => c.toUpperCase());
	const metadataText = connection === 'not_attempted' ? 'Not collected: ' + noDatabase()
		: connection === 'failed' ? 'Collection failed: the database connection failed.'
			: metadataParts.length ? capital(metadataParts.join('; ')) + '.' : 'Not applicable.';
	const planText = connection === 'not_attempted' ? 'Not collected: ' + noDatabase()
		: connection === 'failed' ? 'Collection failed: the database connection failed.'
			: capital(countsText(planCounts, PLAN_COUNT_LABELS) || 'not applicable') + '.';
	const metadataState = connection !== 'connected' ? 'none' : counts.failed ? 'partial' : counts.collected ? 'done' : 'none';
	const planState = connection === 'connected' && planCounts.collected ? 'done'
		: connection === 'connected' && planCounts.failed ? 'partial' : 'none';
	const evidenceRows: EvidenceRow[] = [
		['Static analysis', 'Collected for ' + statements.length + ' statement(s).', 'done'],
		['Table metadata', metadataText, metadataState],
		['Execution plan', planText, planState],
	];

	// Unknowns that apply to this review, each with why it is unknown.
	const unknowns: EvidenceRow[] = [];
	if (writes.length) {
		unknowns.push(['Rows changed', 'Not collected: the statement is never run. A read-only count of the candidate rows estimates it.', 'none']);
		if (writes.some(s => s.scope && s.scope.indirect_effects === 'not_checked'))
			unknowns.push(['Triggers, foreign keys', 'Not checked: triggers and foreign-key actions are not inspected.', 'none']);
	}
	if (statements.length)
		unknowns.push(['Run time, locks', 'Not measured: plain EXPLAIN reports the optimizer plan; it does not measure run time or locks.', 'info']);
	return { statements, limits, findings, actions, evidenceRows: evidenceRows.concat(unknowns), unknowns, top: ranked[0] ?? null };
}

// "1–3, 7" from [1, 2, 3, 7].
export function numberRanges(numbers: number[]): string {
	const parts: [number, number][] = [];
	numbers.forEach((n, i) => {
		const last = parts[parts.length - 1];
		if (i > 0 && last && n === numbers[i - 1]! + 1) last[1] = n;
		else parts.push([n, n]);
	});
	return parts.map(([a, b]) => (a === b ? String(a) : a + '–' + b)).join(', ');
}

// One line per distinct text; with several statements, the statements it applies to.
export function groupedLines(statements: Statement[], line: (s: Statement) => string): string[] {
	if (statements.length <= 1) return statements.map(line);
	const groups = new Map<string, number[]>();
	statements.forEach((s, index) => {
		const value = line(s);
		groups.set(value, [...(groups.get(value) ?? []), index + 1]);
	});
	return [...groups].map(([value, numbers]) => (numbers.length > 1 ? 'Statements ' : 'Statement ') + numberRanges(numbers) + ': ' + value);
}

// Statements that were not analysed (they carry a parse_error).
export const notAnalysedCount = (data: ReviewResponse) => (data.statements ?? []).filter(s => s.parse_error).length;

export function notAnalysedText(data: ReviewResponse, count: number): string {
	const total = data.statements.length;
	return count + ' of ' + total + (total === 1 ? ' statement was' : ' statements were')
		+ ' not analysed: the validator did not assess ' + (count === 1 ? 'its' : 'their')
		+ ' risk. Review ' + (count === 1 ? 'it' : 'them') + ' by hand.';
}

// The one-line review status for a completed review.
export function completionText(data: ReviewResponse): string {
	const missing = notAnalysedCount(data);
	if (missing) return 'Review incomplete. ' + notAnalysedText(data, missing);
	return data.overall_confidence === 'HIGH' ? 'Review complete.'
		: 'Review complete. Confidence is ' + text(data.overall_confidence) + ': read the limitations.';
}

export function countBy(items: Statement[], key: 'statement_type' | 'risk_level'): string {
	const counts = new Map<string, number>();
	items.forEach(item => { const value = text(item[key]); counts.set(value, (counts.get(value) ?? 0) + 1); });
	return [...counts].map(([value, count]) => count + ' ' + value).join(', ');
}
