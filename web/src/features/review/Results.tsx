// The result pane: no review yet, reviewing, failed, or the assessment.
import type { ReviewResponse } from '../../types/review';
import { PUBLIC_DEMO } from '../../shared/environment';
import {
	analysisModeLabel, completionText, evidenceSummaryText, groupedLines, filterLine, keyFinding,
	reviewModel, riskClass, scanLine, text,
} from './reviewText';
import { StatementList, type FocusRequest } from './StatementList';
import type { Reviewed, Run } from './useReview';

const CONFIDENCE_TEXT: Record<string, string> = {
	HIGH: 'Lowest statement confidence. The available evidence supports this assessment.',
	LIMITED: 'Lowest statement confidence. Some evidence is missing: read the limitations.',
	LOW: 'Lowest statement confidence. Little evidence supports this assessment.',
};

export function Results({ run, reviewed, stale, onRetry, focus }: {
	run: Run; reviewed: Reviewed | null; stale: boolean; onRetry: () => void; focus: FocusRequest | null;
}) {
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
	if (!reviewed) return <Initial />;
	return <Assessment data={reviewed.result} at={reviewed.at} stale={stale} focus={focus} />;
}

function Initial() {
	return (
		<div className="panel result-initial">
			<div className="initial-mark" aria-hidden="true"><span /><span /><span /></div>
			<h2>No review yet</h2>
			<p>{PUBLIC_DEMO
				? 'Paste SQL and choose Review SQL. The online demo runs static analysis only; database evidence is available in the downloaded application.'
				: 'Each statement gets a risk level, a confidence and the checks to make before it runs.'}</p>
			<dl className="initial-terms">
				<div><dt>Risk</dt><dd>How much damage the statement could do, from LOW to CRITICAL. The overall score is the highest statement score, not a sum.</dd></div>
				<div><dt>Confidence</dt><dd>How well the evidence supports that assessment. Not a safety rating.</dd></div>
				<div><dt>Evidence</dt><dd>{PUBLIC_DEMO ? 'Not available in the online demo.' : 'Optional: a plain EXPLAIN and table metadata from a database you connect.'}</dd></div>
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

function Assessment({ data, at, stale, focus }: { data: ReviewResponse; at: Date; stale: boolean; focus: FocusRequest | null }) {
	const model = reviewModel(data);
	const top = model.findings[0];
	const leader = model.top;
	const more = (count: number, limit: number) => count > limit ? <li className="muted">+{count - limit} more in the statement details</li> : null;
	return (
		<div className="assessment">
			{stale && <p className="stale" role="status">The SQL or the database context changed after this review. Review again to update it.</p>}
			<section className={'panel verdict ' + riskClass(data.overall_risk_level)} aria-label="Overall assessment">
				<div className="verdict-main">
					<span className="eyebrow">Overall risk</span>
					<div className="verdict-line">
						<strong className="risk-word">{text(data.overall_risk_level)}</strong>
						<span className="score mono">{text(data.overall_score)}<small>/100</small></span>
					</div>
					{leader && (
						<p className="driver">Highest: statement {leader.n} · <span className="mono">{text(leader.s.statement_type)}</span>
							{leader.s.tables && leader.s.tables.length > 0 && <> · <span className="mono">{leader.s.tables.join(', ')}</span></>}
							: {keyFinding(leader.s)}</p>
					)}
					<p className="status-line" role="status">{completionText(data)}</p>
				</div>
				<div className="verdict-side">
					<div>
						<span className="eyebrow">Confidence</span>
						<strong className={'confidence conf-' + (data.overall_confidence ?? 'low').toLowerCase()}>{text(data.overall_confidence)}</strong>
						<p>{CONFIDENCE_TEXT[data.overall_confidence ?? ''] ?? 'Not reported.'}</p>
					</div>
					<div>
						<span className="eyebrow">Analysis</span>
						<strong className="mode">{analysisModeLabel(data.analysis_mode)}</strong>
						<p className="evidence-summary">{evidenceSummaryText(data.evidence_summary)}</p>
					</div>
					<div className="verdict-facts">
						<span>{text(data.statement_count)} {data.statement_count === 1 ? 'statement' : 'statements'}</span>
						<time dateTime={at.toISOString()}>Reviewed {at.toLocaleTimeString()}</time>
					</div>
				</div>
			</section>

			<section className="panel assess" aria-label="Primary assessment">
				<AssessRow label="Top finding">
					{top
						? <li><span className={'pill ' + riskClass(top.level)}>{top.level}</span> <a href={'#statement-' + top.n}>{top.text.replace(/ \(\+\d+\)$/, '')}</a> <span className="mono muted">+{top.points}</span></li>
						: <li className="muted">No rule raised the score. This does not mean the SQL is safe.</li>}
				</AssessRow>
				<AssessRow label="Execution plan">{groupedLines(model.statements, scanLine).map((line, i) => <li key={i}>{line}</li>)}</AssessRow>
				<AssessRow label="Filter and index">{groupedLines(model.statements, filterLine).map((line, i) => <li key={i}>{line}</li>)}</AssessRow>
				<AssessRow label="Limitations">
					{model.limits.length
						? <>{model.limits.slice(0, 3).map((l, i) => <li key={i}>{l.text}</li>)}{more(model.limits.length, 3)}</>
						: <li className="muted">None identified beyond the unknowns below.</li>}
				</AssessRow>
				<AssessRow label="Unknown">{model.unknowns.map(([term, detail]) => <li key={term}>{term}: {detail}</li>)}</AssessRow>
				<AssessRow label="Check first">
					{model.actions.length
						? <>{model.actions.slice(0, 3).map((a, i) => <li key={i}>{a.text}</li>)}{more(model.actions.length, 3)}</>
						: <li className="muted">No review actions reported.</li>}
				</AssessRow>
			</section>

			<StatementList statements={model.statements} focus={focus} />
		</div>
	);
}

function AssessRow({ label, children }: { label: string; children: React.ReactNode }) {
	return (
		<div className="assess-row">
			<h3>{label}</h3>
			<ul>{children}</ul>
		</div>
	);
}
