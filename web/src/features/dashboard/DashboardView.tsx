// The current review at a glance. Values come only from the last response on this
// page; nothing is counted across reviews.
import { PUBLIC_DEMO } from '../../shared/environment';
import { countBy, notAnalysedCount, notAnalysedText, reviewModel, riskClass, scopeLine, text } from '../review/reviewText';
import type { ReviewStore } from '../review/useReview';

export function DashboardView({ review, onStart, onOpenReview, onOpenStatement }: {
	review: ReviewStore; onStart: () => void; onOpenReview: () => void; onOpenStatement: (n: number) => void;
}) {
	const running = review.run.status === 'running';
	const failed = review.run.status === 'failed' ? review.run.message : null;
	// While a review runs, or after it failed, the previous result is not shown as current.
	const data = running || failed ? null : review.reviewed?.result ?? null;
	const model = data ? reviewModel(data) : null;
	const missing = data ? notAnalysedCount(data) : 0;
	const limited = data !== null && data.overall_confidence !== 'HIGH';

	const statusTile = running ? ['In progress', 'Waiting for the validator.', 'running']
		: failed ? ['Failed', failed, 'failed']
			: data && missing ? ['Incomplete analysis', notAnalysedText(data, missing), 'warning']
				: data ? [limited ? 'Complete, with limitations' : 'Complete',
					limited ? 'Read the limitations before relying on the result.' : 'All statements were assessed.', limited ? 'warning' : 'complete']
					: ['No review yet', 'Start a SQL review to see its assessment here.', ''];
	const none = running ? 'Pending' : 'No result';

	return (
		<section className="view dashboard" aria-labelledby="dashboardTitle">
			<header className="page-head">
				<div>
					<h1 id="dashboardTitle">Dashboard</h1>
					<p>Risk, confidence and evidence for the SQL you are reviewing now.</p>
				</div>
				<button type="button" className="btn btn-primary" onClick={onStart}>New SQL review</button>
			</header>

			<div className="tiles">
				<div className={'panel tile tile-risk ' + (data ? riskClass(data.overall_risk_level) : '')}>
					<span className="eyebrow">Overall risk</span>
					{data
						? <strong><span className="risk-text">{text(data.overall_risk_level)}</span> <span className="mono">{text(data.overall_score)} / 100</span></strong>
						: <strong className="muted">{none}</strong>}
					<small>Highest statement score; not a sum or an average.</small>
				</div>
				<div className="panel tile">
					<span className="eyebrow">Confidence</span>
					<strong className={data ? '' : 'muted'}>{data ? text(data.overall_confidence) : none}</strong>
					<small>Lowest statement confidence. How well the evidence supports the assessment; not a safety rating.</small>
				</div>
				<div className="panel tile">
					<span className="eyebrow">Statements</span>
					<strong className={model ? '' : 'muted'}>{model ? model.statements.length : none}</strong>
					<small>{model ? (countBy(model.statements, 'statement_type') || 'None') + '. By risk: ' + (countBy(model.statements, 'risk_level') || 'none') + '.'
						: 'Up to 16 MySQL statements per review.'}</small>
				</div>
				<div className={'panel tile state-' + statusTile[2]} role="status">
					<span className="eyebrow">Review status</span>
					<strong>{statusTile[0]}</strong>
					<small>{statusTile[1]}</small>
				</div>
			</div>

			{!model && !running && !failed && (
				<div className="panel start">
					<div>
						<h2>Start a SQL review</h2>
						<p>Paste the SQL from a ticket. Each statement is classified and scored, with how confident that assessment
							is and what to check before you run it. Your SQL is never executed.</p>
					</div>
					<ol className="start-steps">
						<li><b>Paste SQL</b><span>up to 16 statements</span></li>
						{!PUBLIC_DEMO && <li><b>Connect (optional)</b><span>adds plan and metadata evidence</span></li>}
						<li><b>Read</b><span>risk, confidence, findings</span></li>
					</ol>
				</div>
			)}
			{failed && (
				<div className="panel result-state" role="alert">
					<p>{failed}</p>
					<button type="button" className="btn btn-secondary" onClick={onOpenReview}>Back to SQL review</button>
				</div>
			)}

			{model && data && (
				<>
					{model.top && (
						<p className="headline"><b>Highest risk: statement {model.top.n} · {text(model.top.s.statement_type)}
							{model.top.s.tables && model.top.s.tables.length ? ' · ' + model.top.s.tables.join(', ') : ''}.</b>{' '}
							<span>{scopeLine(model.top.s) ?? ''}</span></p>
					)}
					<div className="dash-grid">
						<section className="panel dash-card" aria-labelledby="findingsTitle">
							<h2 id="findingsTitle">Priority findings</h2>
							{model.findings.length ? (
								<table className="data-table">
									<thead><tr><th scope="col">Severity</th><th scope="col">Statement</th><th scope="col">Finding</th><th scope="col" className="num">Points</th></tr></thead>
									<tbody>
										{model.findings.slice(0, 8).map((f, i) => (
											<tr key={i} className={riskClass(f.level)}>
												<td><span className="pill">{f.level}</span></td>
												<td className="mono">#{f.n}</td>
												<td><a href={'#statement-' + f.n} onClick={e => { e.preventDefault(); onOpenStatement(f.n); }}>{f.text.replace(/^Statement \d+: /, '').replace(/ \(\+\d+\)$/, '')}</a></td>
												<td className="num mono">+{f.points}</td>
											</tr>
										))}
									</tbody>
								</table>
							) : <p className="muted">No rule raised the score. This does not mean the SQL is safe.</p>}
							<div className="card-foot"><button type="button" className="btn btn-secondary btn-sm" onClick={onOpenReview}>Open full review</button></div>
						</section>
						<div className="dash-side">
							<section className="panel dash-card" aria-labelledby="actionsTitle">
								<h2 id="actionsTitle">DBA actions</h2>
								{model.actions.length
									? <ol className="actions-list">{model.actions.slice(0, 6).map((a, i) => <li key={i}>{a.text}</li>)}</ol>
									: <p className="muted">No review actions reported.</p>}
							</section>
							<section className="panel dash-card" aria-labelledby="evidenceTitle">
								<h2 id="evidenceTitle">Evidence</h2>
								<dl className="evidence-table">
									{model.evidenceRows.map(([term, detail, coverage]) => (
										<div key={term}><dt className={'cov-' + coverage}>{term}</dt><dd>{detail}</dd></div>
									))}
								</dl>
							</section>
							<section className="panel dash-card" aria-labelledby="limitsTitle">
								<h2 id="limitsTitle">Limitations</h2>
								{model.limits.length
									? <ul className="plain">{model.limits.map((l, i) => <li key={i}>{l.text}</li>)}</ul>
									: <p className="muted">None identified beyond the unknowns above.</p>}
							</section>
						</div>
					</div>
				</>
			)}
		</section>
	);
}
