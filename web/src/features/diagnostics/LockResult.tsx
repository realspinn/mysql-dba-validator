// InnoDB lock waits on the selected database's tables: who waits, who blocks, on which
// table and index. Read only; it never acts on a session.
import type { LockSide, LockSnapshot } from '../../types/diagnostics';
import { formatSeconds } from './snapshotText';

function Session({ side }: { side: LockSide }) {
	return (
		<div className="lock-session">
			<strong>{side.session_id == null ? 'Connection not verified' : 'Connection ' + side.session_id}</strong>
			<span className="muted">Thread {side.thread_id}</span>
		</div>
	);
}

export function LockResult({ data }: { data: LockSnapshot }) {
	return (
		<div className="snapshot-result">
			<div className="panel">
				<div className="result-head">
					<h2>Lock waits ({data.waits.length})</h2>
					<p id="lockRankNote">One row for each waiting lock and a lock that blocks it, at the time shown. A blocking
						session may already have finished: refresh before acting on it.
						{data.more_waits ? ' More waits exist; only the first ' + data.limit + ' are shown.' : ''}</p>
				</div>
				<ul className="notes">
					{data.sessions === 'unverified' && <li>Connection IDs are not verified: your account cannot read performance_schema.threads.</li>}
					<li>Only InnoDB row and table lock waits on this database's tables. Metadata locks and other contention are not included.</li>
					<li>Time in state is whole seconds in the session's current state: for a waiting session, about how long it has waited; for an idle blocker, how long it has been idle.</li>
				</ul>
				<div className="table-wrap">
					<table className="data-table lock-table" aria-describedby="lockRankNote">
						<caption className="visually-hidden">InnoDB lock waits for {data.schema}</caption>
						<thead><tr>
							<th scope="col">Waiting session</th><th scope="col" className="num">Time in state</th>
							<th scope="col">Blocking session</th><th scope="col" className="num">Time in state</th>
							<th scope="col">Table and index</th><th scope="col">Lock</th>
						</tr></thead>
						<tbody>
							{data.waits.map((w, i) => (
								<tr key={i}>
									<td data-label="Waiting session"><Session side={w.waiting} /></td>
									<td data-label="Waiting session time in state" className="num mono">{formatSeconds(w.waiting.seconds_in_state)}</td>
									<td data-label="Blocking session"><Session side={w.blocking} /></td>
									<td data-label="Blocking session time in state" className="num mono">{formatSeconds(w.blocking.seconds_in_state)}</td>
									<td data-label="Table and index"><div className="lock-where">
										<code>{w.table + (w.partition ? ' (' + w.partition + ')' : '')}</code>
										<span className="muted">{w.index ? 'Index ' + w.index : 'No index (table lock)'}</span>
									</div></td>
									<td data-label="Lock"><div className="lock-mode">
										<span>{w.lock_type === 'RECORD' ? 'Row lock' : 'Table lock'}</span>
										<span className="muted">Requested {w.requested_mode}</span>
										<span className="muted">Blocked by {w.blocking_mode}</span>
									</div></td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			</div>
		</div>
	);
}
