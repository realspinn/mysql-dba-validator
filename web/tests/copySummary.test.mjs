// Tests for the Copy review summary text and its rules (src/features/review/copySummary.ts).
// Run with `npm test` (Node's built-in runner; no test dependency). Node reads the
// TypeScript source directly, so there is no build step between code and test.
//
// The fixtures in tests/fixtures are real /api/validate responses from the backend
// (static analysis). Database evidence cannot be produced without a MySQL server, so
// those cases change a real response the way the documented response shape allows
// (src/types/review.ts); they are synthetic and say so.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { copySummaryFor, reviewSummaryText, writeToClipboard } from '../src/features/review/copySummary.ts';

const load = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), 'utf8'));
const clone = value => structuredClone(value);
const SINGLE = load('single_select');
const BATCH = load('batch');
const NOT_ANALYSED = load('not_analysed');
const UPDATE_ALL = load('update_all');

// Words from the submitted SQL (see the fixtures) that must never be copied.
const SUBMITTED_WORDS = ['customers_secret', 'orders_private', 'audit_log', 'secret-region', 'FROB', 'secret_token_xyz',
	'accounts', 'balance', 't1', 't2', 'email'];
function assertNoSql(text, sql) {
	for (const word of SUBMITTED_WORDS) assert.ok(!text.includes(word), `copied text contains ${word}`);
	assert.ok(!text.includes(sql), 'copied text contains the submitted SQL');
}

const idle = { status: 'idle' };
const reviewed = (response, sql = '') => ({ result: response, sql, context: 'local||', at: new Date(0) });

// ------------------------------------------------------------------ content

test('a normal review: name, risk, score, confidence, count, mode and the not-approval statement', () => {
	const r = SINGLE.response;
	const text = reviewSummaryText(r);
	assert.match(text, /^MySQL DBA Validator: SQL review summary\n/);
	assert.ok(text.includes(`Overall risk: ${r.overall_risk_level}, score ${r.overall_score}/100`));
	assert.ok(text.includes(`Overall confidence: ${r.overall_confidence}.`));
	assert.ok(text.includes(`Statements reviewed: ${r.statement_count}`));
	assert.ok(text.includes('Analysis mode: Static analysis'));
	assert.ok(text.includes('Assessment based on SQL structure only.'));
	assert.ok(text.includes('not approval to execute the SQL'));
	assert.ok(text.includes('not proof that it is safe'));
	assertNoSql(text, SINGLE.sql);
});

test('risk and confidence stay separate, and no statement claims the SQL is safe', () => {
	const text = reviewSummaryText(SINGLE.response);
	assert.ok(text.includes('Overall risk: LOW'));
	assert.ok(text.includes('Overall confidence: HIGH.'));
	assert.ok(text.includes('Confidence is separate from risk.'));
	assert.ok(!/\bis safe\b|\bare safe\b|\bsafe to (run|execute)\b/i.test(text.replaceAll('not proof that it is safe', '').replaceAll('does not mean the SQL is safe', '')));
});

test('a batch keeps the submitted order, numbers and types, and marks the highest risk once', () => {
	const r = BATCH.response;
	const text = reviewSummaryText(r);
	const positions = r.statements.map((s, i) => text.indexOf(`${i + 1}. ${s.statement_type}`));
	assert.ok(positions.every(p => p >= 0), positions.join());
	assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
	assert.equal(text.split('(highest risk)').length - 1, 1);
	assert.match(text, /Highest risk: statement \d \((DELETE|DROP)\), CRITICAL, score \d+/);
	r.statements.forEach((s, i) => {
		const line = text.split('\n').find(l => l.startsWith(`${i + 1}. `));
		assert.ok(line.includes(`${s.risk_level} risk, score ${s.score}, confidence ${s.confidence}`), line);
	});
	assertNoSql(text, BATCH.sql);
});

test('every risk level and both reduced confidences are reported as the response says', () => {
	for (const lvl of ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']) {
		for (const conf of ['HIGH', 'LIMITED', 'LOW']) {
			const r = clone(SINGLE.response);
			r.overall_risk_level = lvl;
			r.overall_confidence = conf;
			r.statements[0].risk_level = lvl;
			r.statements[0].confidence = conf;
			const text = reviewSummaryText(r);
			assert.ok(text.includes(`Overall risk: ${lvl},`), lvl);
			assert.ok(text.includes(`Overall confidence: ${conf}.`), conf);
			assert.ok(text.includes(`1. SELECT: ${lvl} risk, score ${r.statements[0].score}, confidence ${conf}`));
		}
	}
	// Real responses: LIMITED (batch) and LOW (a statement that could not be analysed).
	assert.ok(reviewSummaryText(BATCH.response).includes('Overall confidence: LIMITED.'));
	assert.ok(reviewSummaryText(NOT_ANALYSED.response).includes('Overall confidence: LOW.'));
});

test('row scope is reported without a table name', () => {
	const text = reviewSummaryText(UPDATE_ALL.response);
	assert.ok(text.includes('Row scope: every row of the target table'));
	assert.ok(text.includes('Triggers and foreign-key actions were not checked.'));
	assertNoSql(text, UPDATE_ALL.sql);
});

test('static analysis without a connection says no database evidence was collected', () => {
	const text = reviewSummaryText(SINGLE.response);
	assert.ok(text.includes('Execution plan: not collected. Table metadata: not collected.'));
	assert.ok(text.includes('No database evidence: the assessment is based on the SQL text only.'));
	assert.ok(text.includes('is not a positive result'));
});

// Synthetic: the documented evidence shape, applied to a real response.
function withEvidence({ connection, plan, metadata, overall, mode, summary, tables }) {
	const r = clone(SINGLE.response);
	r.analysis_mode = mode;
	r.evidence_summary = summary;
	r.statements[0].evidence = { connection, plan, metadata: { ...metadata, tables }, overall };
	r.statements[0].database_evidence = { access_type: 'ref', key: 'idx_hidden_key', estimated_rows: 1234, extra: 'Using where; hidden_extra', full_table_scan: false };
	return r;
}

test('collected database evidence is reported as collected, as estimates, without names', () => {
	const r = withEvidence({
		connection: { state: 'connected' }, plan: { state: 'collected', interpreted: true, rows: 1 }, metadata: { state: 'collected' },
		overall: 'collected', mode: 'DATABASE_EVIDENCE', tables: [{ name: 'customers_secret', found: true }],
		summary: { connection: { state: 'connected' }, plan: { collected: 1 }, metadata: { collected: 1 } },
	});
	const text = reviewSummaryText(r);
	assert.ok(text.includes('Analysis mode: Database evidence'));
	assert.ok(text.includes('Execution plan: collected (optimizer estimates, not rows changed). Table metadata: collected.'));
	assert.ok(text.includes('Execution plan: 1 collected · Metadata: 1 collected'));
	assert.ok(!text.includes('idx_hidden_key') && !text.includes('hidden_extra') && !text.includes('customers_secret'));
	assert.ok(!text.includes('1234'), 'the plan details are not copied');
});

test('a table that was not found, or could not be checked, is reported without its name', () => {
	const r = withEvidence({
		connection: { state: 'connected' }, plan: { state: 'failed', reason: 'explain_failed' }, metadata: { state: 'collected' },
		overall: 'partial', mode: 'DATABASE_EVIDENCE', tables: [{ name: 'customers_secret', found: false }, { name: 'other_hidden', found: null }],
		summary: { connection: { state: 'connected' }, plan: { failed: 1 }, metadata: { collected: 1 }, partial: true },
	});
	const text = reviewSummaryText(r);
	assert.ok(text.includes('Statement 1: a referenced table was not found in the selected database.'));
	assert.ok(text.includes('Statement 1: whether a referenced table exists could not be determined.'));
	assert.ok(text.includes('Statement 1: the execution plan could not be collected.'));
	assert.ok(text.includes('Execution plan: could not be collected.'));
	assert.ok(!text.includes('customers_secret') && !text.includes('other_hidden'));
});

test('unavailable database evidence is not presented as collected or as a good result', () => {
	const r = withEvidence({
		connection: { state: 'failed', reason: 'unreachable' }, plan: { state: 'not_attempted' }, metadata: { state: 'not_attempted' },
		overall: 'unavailable', mode: 'STATIC_DATABASE_UNAVAILABLE', tables: [],
		summary: { connection: { state: 'failed' } },
	});
	const text = reviewSummaryText(r);
	assert.ok(text.includes('Analysis mode: Static + database evidence unavailable'));
	assert.ok(text.includes('Database evidence could not be collected.'));
	assert.ok(text.includes('The database could not be reached.'));
	assert.ok(text.includes('The database connection failed: the assessment is based on the SQL text only.'));
	assert.ok(text.includes('Execution plan: not collected. Table metadata: not collected.'));
	assert.ok(!text.includes('Execution plan: collected'));
});

test('evidence that does not apply is not reported as unavailable', () => {
	const r = withEvidence({
		connection: { state: 'connected' }, plan: { state: 'not_supported' }, metadata: { state: 'not_applicable' },
		overall: 'not_applicable', mode: 'DATABASE_EVIDENCE', tables: [],
		summary: { connection: { state: 'connected' }, plan: { not_supported: 1 }, metadata: { not_applicable: 1 } },
	});
	assert.ok(reviewSummaryText(r).includes('Execution plan: not applicable. Table metadata: not applicable.'));
});

test('a statement that could not be analysed says so, with a fixed reason and no parser text', () => {
	const r = NOT_ANALYSED.response;
	const text = reviewSummaryText(r);
	assert.ok(text.includes('2. UNKNOWN (highest risk): not analysed (not valid MySQL). The validator did not assess its risk.'));
	assert.ok(text.includes('Highest risk: statement 2 (UNKNOWN), CRITICAL: not analysed, so its risk was not assessed'));
	assert.ok(text.includes('Review incomplete. 1 of 3 statements were not analysed'));
	assert.ok(text.includes('Statement 2: not analysed: not valid MySQL.'));
	assert.ok(!text.includes('cannot begin with'), 'the parser message is not copied');
	assertNoSql(text, NOT_ANALYSED.sql);
});

test('missing optional fields are reported as not reported, never as values', () => {
	assert.doesNotThrow(() => reviewSummaryText({ statements: [] }));
	const empty = reviewSummaryText({ statements: [] });
	assert.ok(empty.includes('Overall risk: not reported, score not reported/100'));
	assert.ok(empty.includes('No statements were reported.'));
	const bare = reviewSummaryText({ statements: [{ sql: 'x' }] });
	assert.ok(bare.includes('1. UNKNOWN: not reported risk, score not reported, confidence not reported'));
	assert.ok(bare.includes('Overall confidence: not reported.'));
	assert.ok(!bare.includes('undefined') && !bare.includes('null') && !bare.includes('NaN'));
});

test('unexpected values are not copied: levels, numbers and statement types are checked', () => {
	const r = clone(SINGLE.response);
	r.overall_risk_level = 'LOW; DROP TABLE secret_one';
	r.overall_score = 'pwd=secret_two';
	r.statements[0].statement_type = 'SELECT secret_three FROM t';
	r.statements[0].confidence = 'secret_four';
	const text = reviewSummaryText(r);
	for (const value of ['secret_one', 'secret_two', 'secret_three', 'secret_four']) assert.ok(!text.includes(value), value);
});

// ------------------------------------------------------------------ privacy

test('sensitive values placed in every field of the response are never copied', () => {
	const r = clone(BATCH.response);
	const SECRETS = ['hunter2-password', 'db-host.internal.example', 'svc_account_user', 'tok_abcdef123456', '/etc/ssl/private/ca.pem',
		'idx_hidden_key', 'hidden_column', 'hidden_schema', 'mysql://user:pw@host/db', 'SELECT secret_literal'];
	r.password = SECRETS[0]; r.host = SECRETS[1]; r.username = SECRETS[2]; r.session_token = SECRETS[3]; r.tls_ca = SECRETS[4];
	r.connection_string = SECRETS[8]; r.database = SECRETS[6];
	r.statements.forEach((s, i) => {
		s.sql = `${SECRETS[9]}_${i}`;
		s.parse_error = `near ${SECRETS[9]}`;
		s.reasons = [`WHERE clause (${SECRETS[5]} = '${SECRETS[0]}') may match most or all rows.`];
		s.tables = [SECRETS[6]];
		s.columns_in_where = [SECRETS[6]];
		s.where_predicate = `${SECRETS[5]} = 1`;
		s.scope = { basis: 'at_most_one_row', table: `${SECRETS[7]}.t`, index: SECRETS[5], indirect_effects: 'not_checked' };
		s.filter_index = { state: 'no_matching_index', table: SECRETS[7], column: SECRETS[6] };
		s.database_evidence = { key: SECRETS[5], extra: SECRETS[0], possible_keys: [SECRETS[5]] };
		s.factors = [...s.factors, { label: `Unique key ${SECRETS[5]} limits the match to at most one row`, points: 3 },
			{ label: `Contact ${SECRETS[2]} about ${SECRETS[0]}`, points: 9 }];
		s.checklist = [...(s.checklist ?? []), `Check ${SECRETS[0]}\n${SECRETS[9]}`, 'x'.repeat(500) + SECRETS[3]];
		s.evidence = { connection: { state: 'connected' }, plan: { state: 'collected', interpreted: true },
			metadata: { state: 'collected', tables: [{ name: SECRETS[6], found: true }] }, overall: 'collected' };
		s.parse_error = i === 1 ? `near ${SECRETS[9]}` : null;
		if (i === 1) s.analysis_status = 'invalid';
	});
	const text = reviewSummaryText(r);
	for (const value of SECRETS) assert.ok(!text.includes(value), `copied text contains ${value}`);
		assert.ok(/finding\(s\) whose wording is not copied/.test(text), 'unknown wording is counted, not printed');
	assert.ok(!text.includes('Contact '), 'unknown finding wording is not printed');
	assertNoSql(text, BATCH.sql);
});

test('a unique-key finding is copied without the key name', () => {
	const r = clone(SINGLE.response);
	r.statements[0].factors = [{ label: 'Unique key idx_hidden_key limits the match to at most one row', points: 4 }];
	const text = reviewSummaryText(r);
	assert.ok(text.includes('- Statement 1 (LOW): A unique key limits the match to at most one row (+4)'));
	assert.ok(!text.includes('idx_hidden_key'));
});

test('the summary is built from approved fields only: no field outside the list changes it', () => {
	const r = clone(BATCH.response);
	const before = reviewSummaryText(r);
	r.statements.forEach(s => {
		s.sql = 'other sql';
		s.tables = ['other'];
		s.reasons = ['other reason'];
		s.where_predicate = 'other';
		s.columns_in_where = ['other'];
		s.optimization = { status: 'advised', recommendations: [{ title: 'other', observation: 'other' }] };
	});
	assert.equal(reviewSummaryText(r), before);
});

// ------------------------------------------------------------------ when copying is allowed

test('copying is offered for a current, completed review only', () => {
	const current = reviewed(BATCH.response, BATCH.sql);
	assert.equal(copySummaryFor(idle, current, false), reviewSummaryText(BATCH.response));
});

test('a stale result (SQL or connection context changed) is not copied', () => {
	// ReviewView sets stale when the reviewed SQL or context differs from the page's.
	assert.equal(copySummaryFor(idle, reviewed(BATCH.response, BATCH.sql), true), null);
});

test('nothing is copied while a review is running, after a failed one, or before any review', () => {
	const current = reviewed(BATCH.response, BATCH.sql);
	assert.equal(copySummaryFor({ status: 'running' }, current, false), null);
	assert.equal(copySummaryFor({ status: 'failed', message: 'Validation failed.' }, current, false), null);
	assert.equal(copySummaryFor(idle, null, false), null);
});

// ------------------------------------------------------------------ clipboard

test('a successful copy writes exactly the text and reports success', async () => {
	const written = [];
	const ok = await writeToClipboard({ writeText: async text => { written.push(text); } }, 'summary');
	assert.equal(ok, true);
	assert.deepEqual(written, ['summary']);
});

test('a refused copy, a missing Clipboard API or a throwing one reports failure', async () => {
	assert.equal(await writeToClipboard({ writeText: async () => { throw new DOMException('denied', 'NotAllowedError'); } }, 's'), false);
	assert.equal(await writeToClipboard({ writeText: () => { throw new Error('sync failure'); } }, 's'), false);
	assert.equal(await writeToClipboard(undefined, 's'), false);
	assert.equal(await writeToClipboard({}, 's'), false);
});
