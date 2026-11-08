// One row per statement, in submitted order; each opens its full detail: the final
// result, score reasoning, database evidence, plan and index assessment, and
// optimization advice.
import { useEffect, useState } from 'react';
import type { Factor, Statement } from '../../types/review';
import {
	checklistText, evidenceView, factorLabel, keyFinding, metadataSummary, OPTIMIZATION_RULES,
	optimizationSummary, pointsText, riskClass, scanLine, filterLine, scopeLine, text,
} from './reviewText';

export interface FocusRequest { n: number; seq: number }
let lastFocusSeq = 0;

export function StatementList({ statements, focus }: { statements: Statement[]; focus: FocusRequest | null }) {
	// Several can be open at once, to compare statements; a single statement starts open.
	const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set(statements.length === 1 ? [1] : []));
	const toggle = (n: number) => setOpen(now => {
		const next = new Set(now);
		if (!next.delete(n)) next.add(n);
		return next;
	});

	// Scrolled to and focused after React has rendered it open (not on an animation
	// frame, which a browser may hold back while the page is not painting).
	const [shown, setShown] = useState<{ n: number } | null>(null);
	const show = (n: number) => {
		setOpen(now => new Set(now).add(n));
		setShown({ n });
	};
	useEffect(() => {
		if (!shown) return;
		const row = document.getElementById('statement-' + shown.n);
		row?.scrollIntoView({ block: 'start' });
		row?.querySelector<HTMLButtonElement>('.statement-row')?.focus({ preventScroll: true });
	}, [shown]);
	// A statement asked for from the dashboard.
	// Applied once: the list is rebuilt when the review view is reopened or a new review
	// runs, and an old request must not open a statement again.
	useEffect(() => {
		if (!focus || focus.seq === lastFocusSeq) return;
		lastFocusSeq = focus.seq;
		show(focus.n);
	}, [focus]);
	// Links to "#statement-N" on this page (the top finding) open that statement.
	useEffect(() => {
		const follow = (event: MouseEvent) => {
			const link = (event.target as Element | null)?.closest?.('.assessment a[href^="#statement-"]');
			if (!link) return;
			event.preventDefault();
			show(Number(link.getAttribute('href')!.slice('#statement-'.length)));
		};
		document.addEventListener('click', follow);
		return () => document.removeEventListener('click', follow);
	}, []);

	return (
		<section className="panel statements" aria-labelledby="statementsHeading">
			<div className="statements-head">
				<h2 id="statementsHeading">Statements</h2>
				<span>In the order submitted · select one for its detail</span>
			</div>
			<ol>
				{statements.map((s, index) => (
					<StatementItem key={index} s={s} n={index + 1} open={open.has(index + 1)} onToggle={() => toggle(index + 1)} />
				))}
			</ol>
		</section>
	);
}

function StatementItem({ s, n, open, onToggle }: { s: Statement; n: number; open: boolean; onToggle: () => void }) {
	const id = 'statement-detail-' + n;
	return (
		<li id={'statement-' + n} className={'statement ' + riskClass(s.risk_level) + (open ? ' open' : '')}>
			<button type="button" className="statement-row" aria-expanded={open} aria-controls={id} onClick={onToggle}>
				<span className="num mono">{n}</span>
				<span className="pill">{text(s.risk_level)}</span>
				<span className="stmt-what">
					<span className="mono">{text(s.statement_type)}{s.tables && s.tables.length ? ' · ' + s.tables.join(', ') : ''}</span>
					<span className="stmt-key">{keyFinding(s)}</span>
				</span>
				<span className="stmt-score mono">{text(s.score)}</span>
				<span className="stmt-conf">{text(s.confidence)}</span>
				<svg viewBox="0 0 24 24" className="chevron" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
			</button>
			<div className="statement-detail" id={id} hidden={!open}>
				{open && <StatementDetail s={s} />}
			</div>
		</li>
	);
}

function FactorList({ factors, empty }: { factors: Factor[] | undefined; empty: string }) {
	if (!factors || !factors.length) return <p className="muted">{empty}</p>;
	return (
		<ul className="factors">
			{factors.map((f, i) => (
				<li key={i}><span>{factorLabel(f)}</span>
					{f.points != null && <span className={'mono points ' + (f.points > 0 ? 'up' : f.points < 0 ? 'down' : 'zero')}>{pointsText(f.points)}</span>}</li>
			))}
		</ul>
	);
}

function Plain({ items, empty }: { items: string[] | undefined; empty: string }) {
	if (!items || !items.length) return <p className="muted">{empty}</p>;
	return <ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul>;
}

function Block({ title, children, className = '' }: { title: string; children: React.ReactNode; className?: string }) {
	return <section className={'detail-block ' + className}><h3>{title}</h3>{children}</section>;
}

function StatementDetail({ s }: { s: Statement }) {
	const evidence = evidenceView(s);
	const meta = metadataSummary(s);
	const scope = scopeLine(s);
	return (
		<div className="detail-inner">
			{s.analysis_text_differs && (
				<p className="warning-line">The SQL could only be read partly. The analysed text below may differ from what you
					submitted; review the original statement by hand.</p>
			)}
			{s.parse_error && <p className="detail-error">{keyFinding(s)}</p>}
			<pre className="sql-block mono">{s.sql}</pre>
			<dl className="detail-metrics">
				<div><dt>Final score</dt><dd className="mono">{s.score} / 100</dd></div>
				<div><dt>Confidence</dt><dd>{text(s.confidence)}</dd></div>
				<div><dt>Static score</dt><dd className="mono">{s.static_score} / 100</dd></div>
			</dl>

			<Block title="Final result" className="final">
				<p>Final risk: {text(s.risk_level)}</p>
				{scope && <p className="row-scope">{scope}</p>}
				<h4>Why</h4>
				<Plain items={s.reasons} empty="No additional reasons reported." />
				<h4>Review checklist</h4>
				<Plain items={(s.checklist ?? []).map(checklistText)} empty="No checklist items reported." />
			</Block>

			<details className="reasoning">
				<summary>Score reasoning: static score {text(s.static_score)}, final score {text(s.score)} (out of 100)</summary>
				<Block title="Static analysis" className="static">
					<p>Static risk: {text(s.static_risk_level)}</p>
					<h4>Static factors</h4>
					<FactorList factors={s.factors} empty="No additional static factors." />
				</Block>
			</details>

			<Block title="Database evidence" className="evidence">
				<p className="evidence-headline">{evidence.headline}</p>
				{evidence.reason && <p className="evidence-reason">{evidence.reason}</p>}
				{evidence.metadata.map((line, i) => <p key={i} className="evidence-metadata">{line}</p>)}
				{evidence.plan && <p className="evidence-plan">{evidence.plan}</p>}
				{evidence.planFields && (
					<dl className="detail-metrics plan-fields">
						{evidence.planFields.map(([term, value]) => <div key={term}><dt>{term}</dt><dd>{value}</dd></div>)}
					</dl>
				)}
				{evidence.planExtra && <p>{evidence.planExtra}</p>}
				<h4>Evidence-based risk adjustment</h4>
				<FactorList factors={s.evidence_factors} empty="No additional database-evidence adjustment." />
				{s.high_estimated_rows_applied != null && <p>High estimated rows applied: {s.high_estimated_rows_applied ? 'Yes' : 'No'}</p>}
			</Block>

			<Block title="Plan and index assessment" className="metadata">
				<p>{meta.status}</p>
				<p>{meta.adjustment}</p>
				<h4>Table scan and estimated row volume</h4>
				<p className="assess-detail">{scanLine(s)}</p>
				{meta.m1.length > 0 && <FactorList factors={meta.m1} empty="" />}
				<h4>Filter and index assessment</h4>
				<p className="assess-detail">{filterLine(s)}</p>
				{meta.m2.length > 0 && <FactorList factors={meta.m2} empty="" />}
			</Block>

			{s.optimization && (
				<details className="reasoning optimization">
					<summary>{optimizationSummary(s.optimization)}</summary>
					<Block title="Optimization" className="optimization">
						{(s.optimization.status === 'not_eligible' || s.optimization.status === 'unavailable')
							? <p className="muted">{s.optimization.reason || 'Optimization advice could not be produced for this statement.'}</p>
							: !(s.optimization.recommendations ?? []).length && <p className="muted">{OPTIMIZATION_RULES}</p>}
						{(s.optimization.recommendations ?? []).map((item, i) => (
							<div key={i} className="advice">
								<h4>{text(item.title)} (observed in the SQL)</h4>
								<p>{item.observation}</p>
								<p>Suggested action: {item.action}</p>
								<p className="muted">Why: {item.reasoning}</p>
								<p className="caveats-label">Caveats:</p>
								<Plain items={item.caveats} empty="No caveats reported." />
								<p className="muted">How to verify: {item.verify}</p>
							</div>
						))}
					</Block>
				</details>
			)}
		</div>
	);
}
