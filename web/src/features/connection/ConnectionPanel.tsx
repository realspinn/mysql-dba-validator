// Database evidence settings: where (Local or an approved company target), the MySQL
// login, and a database chosen from the discovered list. Never shown in the online demo.
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import { targetName } from '../../api/connector';
import { LOCAL_HOST, LOCAL_PORT } from '../../shared/environment';
import { ConnectorPanel } from './ConnectorPanel';
import { useConnectionContext } from './ConnectionContext';
import { StatusLine } from '../../shared/StatusLine';

const LOCAL_OPTION: SelectProps.Option = { label: 'Local (this machine)', value: '', description: `${LOCAL_HOST}:${LOCAL_PORT}` };

export function ConnectionPanel({ open, onToggle }: { open: boolean; onToggle: (open: boolean) => void }) {
	const { state: s, actions } = useConnectionContext();
	const busy = s.busy !== null;
	const targetOptions: SelectProps.Option[] = [LOCAL_OPTION, ...s.targets.map(t => ({
		label: targetName(t), value: t.target_id, description: `${t.host}:${t.port}`,
	}))];
	const databases = s.databases ?? [];
	const databaseOptions = databases.map(name => ({ label: name, value: name }));
	const summary = s.verified ? (s.database ? 'Database: ' + s.database : 'Connected') : 'Optional';

	return (
		<details className="panel connection" open={open} onToggle={e => onToggle(e.currentTarget.open)}>
			<summary>
				<span className="summary-title">Database evidence</span>
				<span className="summary-meta">{summary}</span>
			</summary>
			<div className="connection-body">
				<div className="fields">
					<div className="field field-wide">
						<span id="targetLabel">Target</span>
						<Select ariaLabelledby="targetLabel" options={targetOptions}
							selectedOption={s.target ? targetOptions.find(o => o.value === s.target!.target_id) ?? null : LOCAL_OPTION}
							onChange={e => actions.selectTarget(e.detail.selectedOption.value ?? '')} expandToViewport />
						<p className="hint-text">Local uses MySQL on this computer. Approved company targets appear here after you
							pair the local connector.</p>
					</div>
					<label className="field">
						<span>Username</span>
						<input value={s.username} autoComplete="off" spellCheck={false}
							onChange={e => actions.setLogin({ username: e.target.value })} />
					</label>
					<label className="field">
						<span>Password</span>
						<input type="password" value={s.password} autoComplete="new-password"
							onChange={e => actions.setLogin({ password: e.target.value })} />
					</label>
				</div>
				<div className="row">
					<button type="button" className="btn btn-secondary" onClick={() => void actions.connect()}
						disabled={!s.username || !s.password || busy} aria-busy={busy}>
						{busy ? (s.busy === 'testing' ? 'Testing…' : 'Discovering…') : s.target ? 'Discover databases' : s.verified ? 'Reconnect' : 'Connect'}
					</button>
					<StatusLine value={s.connectionStatus} />
				</div>
				<div className="field">
					<span id="dbLabel">Database</span>
					<Select ariaLabelledby="dbLabel" options={databaseOptions}
						selectedOption={s.database ? { label: s.database, value: s.database } : null}
						onChange={e => actions.selectDatabase(e.detail.selectedOption.value ?? null)}
						disabled={!s.verified && s.busy !== 'discovering'}
						placeholder={s.verified ? 'Choose a database' : 'Connect to list databases'}
						filteringType={databaseOptions.length > 7 ? 'auto' : 'none'}
						filteringPlaceholder="Filter databases" filteringAriaLabel="Filter databases"
						statusType={s.busy === 'discovering' ? 'loading' : 'finished'} loadingText="Listing databases"
						empty="No databases are visible to this login" expandToViewport />
					{s.verified && !s.listComplete && (
						<p className="warning-line" role="alert">Database list is limited. Not all visible databases are shown.</p>
					)}
					<p className="hint-text">Needed only for EXPLAIN and metadata evidence. Each validation uses a fresh database
						connection; the login is sent with each request and never saved.</p>
				</div>
				<ConnectorPanel />
			</div>
		</details>
	);
}
