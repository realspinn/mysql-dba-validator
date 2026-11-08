// Company targets through the local connector: explicit pairing, the approved-target
// list, and a request to add or re-approve a target. The connector window shows each
// request with a one-time code; only that code approves it.
import { useState } from 'react';
import { CONNECTOR_URL } from '../../api/connector';
import { useConnectionContext } from './ConnectionContext';
import { StatusLine } from '../../shared/StatusLine';

const TRUST_OPTIONS = [
	['configured', 'Connector CA bundle (--tls-ca)'],
	['bundle', 'CA bundle file'],
	['system', 'Windows certificate store (Windows only)'],
] as const;

export function ConnectorPanel() {
	const { state: s, actions } = useConnectionContext();
	const [pairingCode, setPairingCode] = useState('');
	const [approvalCode, setApprovalCode] = useState('');
	const [form, setForm] = useState({ name: '', host: '', port: '3306', trust: 'configured' });
	const [caFile, setCaFile] = useState<File | null>(null);
	const paired = s.sessionToken !== null;

	const submitPairing = () => {
		const code = pairingCode;
		setPairingCode(''); // the code is used once and not kept
		void actions.pair(code);
	};
	const submitApproval = () => {
		const code = approvalCode;
		setApprovalCode('');
		void actions.confirmTarget(code);
	};

	return (
		<details className="subpanel" open={paired || undefined}>
			<summary>
				<span className="summary-title">Company targets</span>
				<span className="summary-meta">{paired ? 'Connector paired' : 'Through the local connector'}</span>
			</summary>
			<div className="subpanel-body">
				<p className="hint-text">Remote MySQL is reached only through the local connector on this computer
					({CONNECTOR_URL.replace('http://', '')}). Your login is sent with each request and is not stored.</p>
				{!paired && (
					<div className="fields">
						<label className="field field-wide">
							<span>Connector pairing code</span>
							<input type="password" value={pairingCode} autoComplete="off" spellCheck={false}
								placeholder="Paste the code printed by the local connector"
								onChange={e => setPairingCode(e.target.value)}
								onKeyDown={e => { if (e.key === 'Enter') submitPairing(); }} />
						</label>
					</div>
				)}
				<div className="row">
					{!paired && <button type="button" className="btn btn-secondary" onClick={submitPairing}
						disabled={s.pairing} aria-busy={s.pairing}>Pair connector</button>}
					{paired && <button type="button" className="btn btn-secondary" onClick={() => void actions.loadTargets()}>Reload approved targets</button>}
					{paired && s.reapprovable && s.target && <button type="button" className="btn btn-secondary"
						disabled={s.requestBusy} onClick={() => void actions.requestTarget('reapprove')}>Request re-approval</button>}
					{paired && <button type="button" className="btn btn-quiet" onClick={() => void actions.unpair()}>Unpair</button>}
				</div>
				<StatusLine value={s.sessionStatus} />

				{paired && (
					<div className="add-target" role="group" aria-labelledby="addTargetHeading">
						<h3 id="addTargetHeading">Add a remote connection</h3>
						<p className="hint-text">The connector window shows your request with a one-time approval code. Check the
							host, port, addresses and certificates shown there before you enter the code. No password is sent with
							the request.</p>
						<div className="fields">
							<label className="field">
								<span>Connection name</span>
								<input value={form.name} maxLength={64} autoComplete="off" onChange={e => setForm({ ...form, name: e.target.value })} />
							</label>
							<label className="field">
								<span>Host name</span>
								<input value={form.host} autoComplete="off" spellCheck={false} placeholder="db.example.com"
									onChange={e => setForm({ ...form, host: e.target.value })} />
							</label>
							<label className="field">
								<span>Port</span>
								<input type="number" min={1} max={65535} inputMode="numeric" value={form.port}
									onChange={e => setForm({ ...form, port: e.target.value })} />
							</label>
							<label className="field">
								<span>Trusted certificates</span>
								<select value={form.trust} onChange={e => setForm({ ...form, trust: e.target.value })}>
									{TRUST_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
								</select>
							</label>
							{form.trust === 'bundle' && (
								<label className="field field-wide">
									<span>CA bundle (PEM certificates only, up to 256 KiB)</span>
									<input type="file" accept=".pem,.crt,.cer" onChange={e => setCaFile(e.target.files?.[0] ?? null)} />
								</label>
							)}
							{s.targetRequest && (
								<label className="field">
									<span>Approval code from the connector window</span>
									<input value={approvalCode} autoComplete="off" spellCheck={false} maxLength={16} placeholder="XXXX-XXXX"
										autoFocus onChange={e => setApprovalCode(e.target.value)}
										onKeyDown={e => { if (e.key === 'Enter') submitApproval(); }} />
								</label>
							)}
						</div>
						<div className="row">
							<button type="button" className="btn btn-secondary" disabled={s.requestBusy}
								onClick={() => void actions.requestTarget({ name: form.name, host: form.host, port: Number(form.port), trust: form.trust, caFile })}>
								Request approval
							</button>
							{s.targetRequest && <button type="button" className="btn btn-primary" disabled={s.requestBusy}
								onClick={submitApproval}>Approve</button>}
						</div>
						<StatusLine value={s.requestStatus} />
					</div>
				)}
			</div>
		</details>
	);
}
