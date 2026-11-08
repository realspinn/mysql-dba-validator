import { useState } from 'react';
import type { Review, RiskLevel, StatementResult } from './api';

type Run = { status: 'idle' } | { status: 'running' } | { status: 'failed'; message: string };
type Props = { run: Run; review: { result: Review; at: Date } | null; stale: boolean; onRetry: () => void };

const CONFIDENCE_TEXT = {
	HIGH: 'The available evidence supports this assessment.',
	LIMITED: 'Some evidence is missing: read the limitations.',
	LOW: 'Little evidence supports this assessment.',
} as const;

export function Results({ run, review, stale, onRetry }: Props) {
	if (run.status === 'running') return <Loading />;
	if (run.status === 'failed') {
		return (
			<div className="panel result-state" role="alert">
				<h2>The review did not complete</h2>
				<p>{run.message}</p>
				<button type="button" className="btn btn-secondary" onClick={onRetry}>Try again</button>
			</div>
		);
	}
	if (!review) return <Initial />;
	return <Assessment review={review.result} at={review.at} stale={stale} />;
}

function Initial() {
	return (
		<div className="panel result-initial">
			<div className="initial-mark" aria-hidden="true"><span /><span /><span /></div>
			<h2>No review yet</h2>
			<p>Each statement gets a risk level, a confidence and the checks to make before it runs.</p>
			<dl className="initial-terms">
				<div><dt>Risk</dt><dd>How much damage the statement could do, from LOW to CRITICAL.</dd></div>
				<div><dt>Confidence</dt><dd>How well the evidence supports that assessment. Not a safety rating.</dd></div>
				<div><dt>Evidence</dt><dd>Optional: a plain EXPLAIN and table metadata from a database you connect.</dd></div>
			</dl>
		</div>
	);
}

function Loading() {
	return (
		<div className="panel result-loading" aria-busy="true" role="status" aria-label="Reviewing SQL">
			<div className="progress" aria-hidden="true"><span /></div>
			<p className="loading-label">Reviewing your SQL…</p>
			<div className="skeleton wide" /><div className="skeleton" /><div className="skeleton short" />
		</div>
	);
}

const riskClass = (level: RiskLevel | null) => 'risk-' + (level ?? 'UNKNOWN').toLowerCase();

function Assessment({ review, at, stale }: { review: Review; at: Date; stale: boolean }) {
	const worst = [...review.statements].sort((a, b) => b.score - a.score)[0];
	const top = worst?.factors.slice().sort((a, b) => b.points - a.points)[0];
	return (
		<div className="assessment">
			{stale && <p className="stale" role="status">The SQL or database changed after this review. Review again to update it.</p>}
			<section className={'panel verdict ' + riskClass(review.overallRisk)} aria-label="Overall assessment">
				<div className="verdict-main">
					<span className="eyebrow">Overall risk</span>
					<div className="verdict-line">
						<strong className="risk-word">{review.overallRisk ?? 'NOT ANALYSED'}</strong>
						<span className="score mono">{review.overallScore}<small>/100</small></span>
					</div>
					{worst && (
						<p className="driver">
							Highest: statement {worst.index + 1} · <span className="mono">{worst.statementType}</span>
							{worst.tables.length > 0 && <> · <span className="mono">{worst.tables.join(', ')}</span></>}
							{top && <>: {top.label}</>}
						</p>
					)}
				</div>
				<div className="verdict-side">
					<div>
						<span className="eyebrow">Confidence</span>
						<strong className={'confidence conf-' + (review.overallConfidence ?? 'LOW').toLowerCase()}>{review.overallConfidence ?? 'Unknown'}</strong>
						<p>{review.overallConfidence ? CONFIDENCE_TEXT[review.overallConfidence] : 'Not reported.'}</p>
					</div>
					<div className="verdict-facts">
						<span>{review.analysisMode === 'STATIC' ? 'Static analysis' : 'With database evidence'}</span>
						<span>{review.statementCount} {review.statementCount === 1 ? 'statement' : 'statements'}</span>
						<time dateTime={at.toISOString()}>{at.toLocaleTimeString()}</time>
					</div>
				</div>
			</section>

			<section className="panel statements" aria-label="Statements">
				<div className="statements-head"><h2>Statements</h2><span>In the order submitted · select one for its detail</span></div>
				<ol>{review.statements.map(s => <Statement key={s.index} statement={s} />)}</ol>
			</section>
		</div>
	);
}

function Statement({ statement: s }: { statement: StatementResult }) {
	const [open, setOpen] = useState(false);
	const id = `stmt-${s.index}`;
	return (
		<li className={'statement ' + riskClass(s.riskLevel) + (open ? ' open' : '')}>
			<button type="button" className="statement-row" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
				<span className="num mono">{s.index + 1}</span>
				<span className="pill">{s.riskLevel ?? 'NOT ANALYSED'}</span>
				<span className="stmt-sql mono">{s.sql || '(empty)'}</span>
				<span className="stmt-score mono">{s.score}</span>
				<span className="stmt-conf">{s.confidence ?? '—'}</span>
				<svg viewBox="0 0 24 24" className="chevron" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
			</button>
			<div className="statement-detail" id={id} hidden={!open}>
				<div className="detail-inner">
					{s.parseError && <p className="detail-error">{s.parseError}</p>}
					{s.factors.length > 0 && (
						<div className="detail-block">
							<h3>Why it scored {s.score}</h3>
							<ul className="factors">{s.factors.map((f, i) => <li key={i}><span>{f.label}</span><span className="mono">+{f.points}</span></li>)}</ul>
						</div>
					)}
					{s.reasons.length > 0 && (
						<div className="detail-block"><h3>Limitations</h3><ul>{s.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
					)}
					{s.checklist.length > 0 && (
						<div className="detail-block"><h3>Check before running</h3><ul className="checks">{s.checklist.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
					)}
					<div className="detail-block">
						<h3>Database evidence</h3>
						<p>{s.evidenceMessage ?? (s.evidenceStatus ? s.evidenceStatus.replace(/_/g, ' ') : 'Not collected.')}</p>
					</div>
				</div>
			</div>
		</li>
	);
}
