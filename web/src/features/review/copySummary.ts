// A plain text copy of a completed review, for a ticket or a work discussion.
//
// It is built from an explicit list of fields and fixed wording, never from the whole
// response. Left out on purpose, because they can repeat what the user submitted or
// name their database: the SQL, parser messages (parse_error), the reasons (they can
// quote the WHERE clause), table, column, index and database names, and any login or
// connection detail. A finding or a checklist line is copied only when it is known
// wording (see FIXED_FACTORS); anything else is counted, not printed.
// Pure functions with no DOM, so tests run them directly (web/tests).
import type { Factor, ReviewResponse, Statement } from '../../types/review';
import type { Reviewed, Run } from './useReview';
import {
	CONFIDENCE_TEXT, METADATA_COUNT_LABELS, NOT_ANALYSED_REASONS, PLAN_COUNT_LABELS, allFactors, analysisModeLabel,
	completionText, evidenceHeadline, evidenceOf, evidenceStatusMessage, evidenceSummaryText, factorText, fixed,
	reviewModel,
} from './reviewText.ts';

const LEVELS = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
const CONFIDENCES = ['HIGH', 'LIMITED', 'LOW'];
const NOT_REPORTED = 'not reported';
const level = (value: unknown) => (typeof value === 'string' && LEVELS.includes(value) ? value : NOT_REPORTED);
const confidence = (value: unknown) => (typeof value === 'string' && CONFIDENCES.includes(value) ? value : NOT_REPORTED);
const number = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? String(value) : NOT_REPORTED);
// A statement type is a keyword the parser read (SELECT, CREATE TABLE ...), or UNKNOWN.
const TYPE = '[A-Z][A-Z0-9_ ]{0,30}';
const typeName = (value: unknown) => (typeof value === 'string' && new RegExp('^' + TYPE + '$').test(value) ? value : 'UNKNOWN');

// The factor labels the backend can produce (risk_engine, evidence_scoring,
// metadata_scoring, m2_scoring, validation_results). tests/test_frontend_copy_summary.py
// reads every label from the backend source and fails if one is missing here.
export const FIXED_FACTORS: readonly string[] = [
	'Statement not analysed — risk not assessed',
	'No WHERE clause — potentially affects every row',
	'WHERE clause is always true — matches every row',
	'WHERE clause appears unusually broad',
	'Affected-row count cannot be established from the SQL alone',
	'ALTER drops a column — potentially irreversible schema change',
	'Part of a multi-statement batch',
	'MEDIUM minimum for a SELECT that can lock rows or calls an unreviewed function',
	'EXPLAIN ANALYZE runs the statement it explains',
	'EXPLAIN ANALYZE runs a statement that could not be read',
	'EXPLAIN indicates a full table scan',
	'EXPLAIN estimated rows are high',
	'Metadata supports a large table full-scan write adjustment',
	'Metadata supports a broad full-table scan with no matching leading index',
];
// Labels that name the statement type, or a unique key (the name is not copied).
export const PATTERN_FACTORS: readonly [RegExp, (match: RegExpExecArray) => string][] = [
	[new RegExp('^Base risk for (' + TYPE + ')$'), m => 'Base risk for ' + m[1]],
	[new RegExp('^(' + TYPE + ') is classified as CRITICAL$'), m => m[1] + ' is classified as CRITICAL'],
	[new RegExp('^Explains a (' + TYPE + ') statement$'), m => 'Explains a ' + m[1] + ' statement'],
	[new RegExp('^(' + TYPE + ') changes access privileges$'), m => m[1] + ' changes access privileges'],
	[/^Unique key [^\r\n]{1,200} limits the match to at most one row$/, () => 'A unique key limits the match to at most one row'],
];

// The wording for one factor, or null when it is not known wording.
export function approvedFactor(s: Statement, factor: Factor): string | null {
	const label = typeof factor.label === 'string' ? factor.label : '';
	if (!FIXED_FACTORS.includes(label) && !PATTERN_FACTORS.some(([pattern]) => pattern.test(label))) return null;
	if (label === 'EXPLAIN indicates a full table scan' || label === 'EXPLAIN estimated rows are high') return factorText(s, factor);
	if (label.startsWith('Metadata supports a broad full-table scan'))
		return 'Full scan, and no index starts with the filter column (table metadata)';
	for (const [pattern, wording] of PATTERN_FACTORS) {
		const match = pattern.exec(label);
		if (match) return wording(match);
	}
	return label;
}

interface Finding { n: number; level: string; points: number; text: string }

function findingsOf(statements: Statement[]): { shown: Finding[]; unshown: number } {
	const shown: Finding[] = [];
	let unshown = 0;
	statements.forEach((s, index) => {
		if (s.parse_error) return;
		allFactors(s).filter(f => typeof f.points === 'number' && f.points > 0).forEach(f => {
			const wording = approvedFactor(s, f);
			if (wording === null) unshown++;
			else shown.push({ n: index + 1, level: level(s.risk_level), points: f.points as number, text: wording });
		});
	});
	shown.sort((a, b) => LEVELS.indexOf(b.level) - LEVELS.indexOf(a.level) || b.points - a.points);
	return { shown, unshown };
}

function scopeText(s: Statement): string | null {
	const scope = s.scope;
	if (!scope) return null;
	const indirect = scope.indirect_effects === 'not_checked' ? ' Triggers and foreign-key actions were not checked.' : '';
	if (scope.basis === 'all_rows') return 'Row scope: every row of the target table (from the SQL: no WHERE clause, or one that is always true).' + indirect;
	if (scope.basis === 'at_most_one_row')
		return 'Row scope: at most one row matches directly, by a unique key (confirmed from table metadata). The row may not exist.' + indirect;
	if (scope.basis === 'not_established') return 'Row scope: not established. A WHERE clause can match zero, one, some or all rows.' + indirect;
	return null;
}

function evidenceStates(s: Statement): string {
	const state = evidenceOf(s);
	const plan = fixed(PLAN_COUNT_LABELS, state.plan.state) ?? 'not collected';
	const metadata = fixed(METADATA_COUNT_LABELS, state.metadata.state) ?? 'not collected';
	return 'Execution plan: ' + plan + (state.plan.state === 'collected' ? ' (optimizer estimates, not rows changed)' : '')
		+ '. Table metadata: ' + metadata + '.';
}

// What could not be established, without naming a table (the names are not copied).
function limitations(data: ReviewResponse): string[] {
	const lines: string[] = [];
	const connection = data.evidence_summary?.connection.state ?? 'not_attempted';
	if (connection === 'not_attempted') lines.push('No database evidence: the assessment is based on the SQL text only.');
	if (connection === 'failed') lines.push('The database connection failed: the assessment is based on the SQL text only.');
	(data.statements ?? []).forEach((s, index) => {
		const label = 'Statement ' + (index + 1) + ': ';
		if (s.parse_error) lines.push(label + 'not analysed: ' + (fixed(NOT_ANALYSED_REASONS, s.analysis_status) ?? 'it could not be analysed') + '.');
		else if (s.analysis_text_differs) lines.push(label + 'the SQL could only be read partly.');
		const state = evidenceOf(s);
		if (state.metadata.state === 'collected') {
			const tables = state.metadata.tables ?? [];
			if (tables.some(t => t.found === false)) lines.push(label + 'a referenced table was not found in the selected database.');
			if (tables.some(t => t.found !== true && t.found !== false)) lines.push(label + 'whether a referenced table exists could not be determined.');
		}
		if (state.plan.state === 'failed') lines.push(label + 'the execution plan could not be collected.');
		if (state.metadata.state === 'failed') lines.push(label + 'table metadata could not be collected.');
		if (s.scope && s.scope.basis === 'not_established') lines.push(label + 'the rows it can change are not established.');
	});
	return lines;
}

const MAX_CHECK_LENGTH = 400;
const MAX_CHECKS = 15;

// The review as plain text.
export function reviewSummaryText(data: ReviewResponse): string {
	const model = reviewModel(data);
	const statements = model.statements;
	const count = typeof data.statement_count === 'number' ? data.statement_count : statements.length;
	const out: string[] = [];
	const section = (title: string, lines: string[]) => { out.push('', title, ...lines); };

	out.push('MySQL DBA Validator: SQL review summary', '');
	out.push('Overall risk: ' + level(data.overall_risk_level) + ', score ' + number(data.overall_score) + '/100 (the highest statement score, not a sum)');
	out.push('Overall confidence: ' + confidence(data.overall_confidence) + '. '
		+ (CONFIDENCE_TEXT[data.overall_confidence ?? ''] ?? 'Not reported.') + ' Confidence is separate from risk.');
	out.push('Statements reviewed: ' + number(count));
	out.push('Analysis mode: ' + analysisModeLabel(data.analysis_mode));
	out.push('Status: ' + completionText(data));
	if (model.top) {
		const top = model.top;
		out.push('Highest risk: statement ' + top.n + ' (' + typeName(top.s.statement_type) + '), ' + level(top.s.risk_level)
			+ (top.s.parse_error ? ': not analysed, so its risk was not assessed' : ', score ' + number(top.s.score)));
	}

	const { shown, unshown } = findingsOf(statements);
	section('Key findings', shown.length
		? [...shown.slice(0, 5).map(f => '- Statement ' + f.n + ' (' + f.level + '): ' + f.text + ' (+' + f.points + ')'),
			...(shown.length > 5 ? ['- ' + (shown.length - 5) + ' more in the validator'] : []),
			...(unshown ? ['- ' + unshown + ' finding(s) whose wording is not copied: see the validator'] : [])]
		: [unshown ? '- ' + unshown + ' finding(s) whose wording is not copied: see the validator'
			: '- No rule raised the score. This does not mean the SQL is safe.']);

	section('Statements, in the order submitted', statements.length ? statements.flatMap((s, index) => {
		const head = (index + 1) + '. ' + typeName(s.statement_type)
			+ (statements.length > 1 && model.top && model.top.n === index + 1 ? ' (highest risk)' : '') + ': ';
		if (s.parse_error) {
			return [head + 'not analysed (' + (fixed(NOT_ANALYSED_REASONS, s.analysis_status) ?? 'it could not be analysed')
				+ '). The validator did not assess its risk.'];
		}
		const finding = findingsOf([s]);
		const top = finding.shown[0];
		const lines = [head + level(s.risk_level) + ' risk, score ' + number(s.score) + ', confidence ' + confidence(s.confidence),
			'   ' + evidenceStates(s)];
		const scope = scopeText(s);
		if (scope) lines.push('   ' + scope);
		lines.push('   Main finding: ' + (top ? top.text + ' (+' + top.points + ')'
			: finding.unshown ? 'wording not copied: see the validator'
				: 'No rule raised the score. This does not mean the SQL is safe.'));
		return lines;
	}) : ['No statements were reported.']);

	// Why the connection failed, when one was attempted (not the prompt to enter a login).
	const reason = statements.map(s => evidenceOf(s).connection).find(c => c.state === 'failed' && evidenceStatusMessage(c.reason));
	section('Database evidence', [evidenceSummaryText(data.evidence_summary),
		...(reason ? [evidenceStatusMessage(reason.reason) as string] : []),
		'Collected evidence is optimizer estimates and table metadata. Evidence that was not collected or could not be collected is not a positive result.']);

	const limits = limitations(data);
	section('Limitations and unknowns', [...limits.map(l => '- ' + l), ...model.unknowns.map(([term, detail]) => '- ' + term + ': ' + detail)]);

	const checks = model.actions.map(a => a.text).filter(t => t.length > 0 && t.length <= MAX_CHECK_LENGTH && !/[\r\n]/.test(t));
	section('DBA checks before execution', checks.length
		? [...checks.slice(0, MAX_CHECKS).map(c => '- ' + c), ...(checks.length > MAX_CHECKS ? ['- ' + (checks.length - MAX_CHECKS) + ' more in the validator'] : [])]
		: ['- No review actions reported.']);

	section('Note', ['This assessment is not approval to execute the SQL and not proof that it is safe. The validator never executes SQL. A DBA must make the decision.',
		'Not included: the SQL, table, column, index and database names, hosts, logins and parser messages.']);
	return out.join('\n') + '\n';
}

// The summary a person may copy now, or null: nothing is reviewed yet, the latest
// review is running or failed, or the result no longer matches the SQL and context.
export function copySummaryFor(run: Run, reviewed: Reviewed | null, stale: boolean): string | null {
	if (run.status !== 'idle' || reviewed === null || stale) return null;
	return reviewSummaryText(reviewed.result);
}

// Writes with the browser's Clipboard API. True only once it has actually succeeded.
export async function writeToClipboard(clipboard: { writeText?: (text: string) => Promise<void> } | undefined, text: string): Promise<boolean> {
	if (!clipboard || typeof clipboard.writeText !== 'function') return false;
	try {
		await clipboard.writeText(text);
		return true;
	} catch {
		return false;
	}
}
