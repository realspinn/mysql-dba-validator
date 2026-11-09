// Statement families MySQL recorded (events_statements_summary_by_digest), ranked by
// total recorded time, with the selected family's full detail.
import { useState } from 'react';
import type { Family, PerfSnapshot } from '../../types/diagnostics';
import { exactDuration, formatCount, formatDuration, serverTime } from './snapshotText';

const familyLabel = (f: Family) => f.digest_text || 'Digest ' + f.digest.slice(0, 12) + ' (no text recorded)';

export function PerformanceResult({ data }: { data: PerfSnapshot }) {
	const [selected, setSelected] = useState(0);
	const families = data.families;
	const family = families[selected] ?? families[0]!;
	return (
		<div className="snapshot-result">
			<div className="panel">
				<div className="result-head">
					<h2>Statement families ({families.length})</h2>
					<p id="perfRankNote">Ranked by total recorded time across all executions of each family. {data.more_families
						? 'MySQL recorded more families for this database; only the top ' + data.limit + ' are shown.'
						: 'These are all the families MySQL recorded for this database.'} Not a view of all server activity.</p>
				</div>
				<ul className="notes">
					{data.digest_consumer === 'unverified' && <li>Collection state not verified: your account cannot read performance_schema.setup_consumers.</li>}
					{(data.overflow_executions ?? 0) > 0 && <li>{formatCount(data.overflow_executions)} statements were counted in MySQL's catch-all row because the digest table is full, so these families may not be representative.</li>}
					{families.some(f => f.timing === 'not_recorded') && <li>Some families have no recorded time: MySQL counted their executions without timing them.</li>}
					<li>The validator's own connection statements and evidence queries are recorded here too.</li>
				</ul>
				<div className="table-wrap">
					<table className="data-table perf-table" aria-describedby="perfRankNote">
						<caption className="visually-hidden">Statement families for {data.schema}</caption>
						<thead><tr>
							<th scope="col">Statement family</th><th scope="col" className="num">Executions</th>
							<th scope="col" className="num">Total time</th><th scope="col" className="num opt">Average</th>
							<th scope="col" className="num opt">Maximum</th><th scope="col" className="num opt">Rows examined</th>
							<th scope="col" className="num opt">Rows sent</th><th scope="col" className="opt">Last seen</th>
						</tr></thead>
						<tbody>
							{families.map((f, i) => (
								<tr key={f.digest} className={i === selected ? 'selected' : ''}>
									<td className="family-cell"><button type="button" className="row-button mono" aria-pressed={i === selected}
										aria-controls="perfDetail" onClick={() => setSelected(i)}>{familyLabel(f)}</button></td>
									<td className="num mono">{formatCount(f.executions)}</td>
									<td className="num mono">{formatDuration(f.total_time_ms)}</td>
									<td className="num mono opt">{formatDuration(f.avg_time_ms)}</td>
									<td className="num mono opt">{formatDuration(f.max_time_ms)}</td>
									<td className="num mono opt">{formatCount(f.rows_examined)}</td>
									<td className="num mono opt">{formatCount(f.rows_sent)}</td>
									<td className="opt nowrap mono">{serverTime(f.last_seen)}</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			</div>
			<section className="panel family-detail" id="perfDetail" aria-labelledby="perfDetailHeading" aria-live="polite">
				<h2 id="perfDetailHeading">Statement family</h2>
				<pre className="sql-block mono">{familyLabel(family)}</pre>
				<dl className="detail-list">
					<dt>Digest</dt><dd className="mono">{family.digest}</dd>
					<dt>Executions</dt><dd>{formatCount(family.executions)}</dd>
					<dt>Total time</dt><dd>{exactDuration(family.total_time_ms)}</dd>
					<dt>Average time</dt><dd>{exactDuration(family.avg_time_ms)}</dd>
					<dt>Maximum time</dt><dd>{exactDuration(family.max_time_ms)}</dd>
					<dt>95th percentile</dt><dd>{family.p95_time_ms == null ? 'Not recorded'
						: exactDuration(family.p95_time_ms) + ' (approximate, a high estimate from MySQL\'s histogram)'}</dd>
					<dt>Lock time</dt><dd>{exactDuration(family.lock_time_ms) + (family.lock_time_ms == null ? '' : ' in total')}</dd>
					<dt>Rows examined</dt><dd>{formatCount(family.rows_examined)}</dd>
					<dt>Rows sent</dt><dd>{formatCount(family.rows_sent)}</dd>
					<dt>Rows affected</dt><dd>{formatCount(family.rows_affected)}</dd>
					<dt>Executions without an index</dt><dd>{formatCount(family.no_index_used)}</dd>
					<dt>First seen</dt><dd>{serverTime(family.first_seen)} (server time)</dd>
					<dt>Last seen</dt><dd>{serverTime(family.last_seen)} (server time)</dd>
				</dl>
				<ul className="notes">
					<li>Totals cover every execution in this family since it was first seen. They are not a measurement of the SQL in your review.</li>
					{family.timing === 'not_recorded' && <li>MySQL counted these executions but recorded no time for them, so time values are not shown.</li>}
					{family.digest_text_truncated && <li>The statement text is shortened to 1024 characters.</li>}
				</ul>
			</section>
		</div>
	);
}
