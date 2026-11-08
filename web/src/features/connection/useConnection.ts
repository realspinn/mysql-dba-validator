// The connection's actions. State lives in one memory-only store; every async flow
// re-reads the latest state when it settles and discards a result whose target, login
// or connector session changed meanwhile.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as connector from '../../api/connector';
import { asDatabaseList, discoverLocalDatabases, testLocalConnection, type DatabaseList } from '../../api/local';
import { RequestError, errorText } from '../../api/http';
import { CONNECTOR_ERROR_MESSAGES, PAIR_AGAIN_GUIDANCE, REMOTE_NOT_AVAILABLE } from '../../api/messages';
import {
	clearedDiscovery, discoveryMatches, initialConnection, LOCAL_START, NOT_PAIRED, status,
	type ConnectionState, type Status,
} from './connectionState';

const SESSION_EXPIRY_NOTICE_SECONDS = 300;
const REAPPROVAL_CODES = ['dns_identity_mismatch', 'target_needs_reapproval'];
const CLOSES_REQUEST = ['target_request_not_found', 'target_request_cancelled', 'dns_changed_since_request',
	'company_target_changed', 'company_target_exists'];
export const MAX_CA_BUNDLE_BYTES = 256 * 1024;

export interface NewTarget { name: string; host: string; port: number; trust: string; caFile: File | null }

export function useConnection() {
	const [state, setState] = useState<ConnectionState>(initialConnection);
	const ref = useRef(state);
	const timers = useRef<number[]>([]);
	const update = useCallback((change: Partial<ConnectionState>) => {
		ref.current = { ...ref.current, ...change };
		setState(ref.current);
	}, []);
	useEffect(() => () => timers.current.forEach(clearTimeout), []);

	// Selecting a target only changes which approved target_id is used. Any change of
	// target clears the login, so a login typed for one target (or for Local) is never
	// sent to another, and a check still running for the old target no longer counts.
	const selectTarget = useCallback((targetId: string) => {
		const s = ref.current;
		const target = s.targets.find(t => t.target_id === targetId) ?? null;
		const login = s.target?.target_id !== target?.target_id ? { username: '', password: '' } : {};
		update({
			target, ...login, ...clearedDiscovery, busy: null, reapprovable: false,
			connectionStatus: status(target ? 'Enter your MySQL username and password, then discover databases.' : LOCAL_START),
		});
	}, [update]);

	const setPaired = useCallback((token: string | null, message: string, kind: Status['kind']) => {
		const s = ref.current;
		const changed = s.sessionToken !== token;
		const change: Partial<ConnectionState> = { sessionToken: token, sessionStatus: status(message, kind) };
		if (changed) {
			timers.current.forEach(clearTimeout);
			timers.current = [];
			// Targets, discovered databases and an open target request belong to one
			// connector session. Only the selected target_id is remembered for the next.
			Object.assign(change, {
				targetRequest: null, reapprovable: false, targets: [],
				rememberedTargetId: s.target ? s.target.target_id : s.rememberedTargetId,
			});
			if (s.target) Object.assign(change, { target: null, username: '', password: '', ...clearedDiscovery, connectionStatus: status(LOCAL_START) });
		}
		update(change);
	}, [update]);

	// Maps a connector failure: a 401 ends the session; a changed address set offers the
	// explicit re-approval request (never automatic). `sent` is the session and target a
	// request was made with: a late answer from an earlier session never ends the current
	// one, and re-approval is offered only for the target that was asked.
	const connectorFailed = useCallback((error: unknown, pairing = false,
		sent?: { token: string | null; targetId: string | null }): string => {
		const now = ref.current;
		const current = !sent || sent.token === now.sessionToken;
		const sameTarget = !sent || sent.targetId === (now.target?.target_id ?? null);
		if (error instanceof RequestError && current) {
			if (error.status === 401 && !pairing) setPaired(null, 'Connector session ended. ' + PAIR_AGAIN_GUIDANCE, 'error');
			if (sameTarget && ref.current.target && error.code && REAPPROVAL_CODES.includes(error.code)) update({ reapprovable: true });
		}
		return errorText(error, 'The request did not complete.');
	}, [setPaired, update]);

	const requireSession = () => {
		const token = ref.current.sessionToken;
		if (!token) throw new RequestError(REMOTE_NOT_AVAILABLE + 'Pair with the local connector first using the pairing code it printed.');
		return token;
	};

	const loadTargets = useCallback(async () => {
		let targets: connector.Target[] = [];
		try {
			targets = await connector.approvedTargets(requireSession());
		} catch (error) {
			update({ sessionStatus: status(connectorFailed(error) || 'Could not load approved targets.', 'error') });
		}
		update({ targets });
		const s = ref.current;
		// A selection that is no longer approved is dropped (and its discovery state).
		if (s.target && !targets.some(t => t.target_id === s.target!.target_id)) selectTarget('');
		else if (!s.target && s.rememberedTargetId) {
			// After re-pairing: reselect the previous target only if still approved.
			const remembered = s.rememberedTargetId;
			update({ rememberedTargetId: null });
			if (targets.some(t => t.target_id === remembered)) selectTarget(remembered);
		}
	}, [connectorFailed, selectTarget, update]);

	const pair = useCallback(async (code: string) => {
		if (!code.trim()) {
			update({ sessionStatus: status('Paste the pairing code printed by the local connector.', 'error') });
			return;
		}
		update({ pairing: true, sessionStatus: status('Pairing…') });
		try {
			const session = await connector.pair(code.trim());
			const minutes = Math.max(1, Math.round(session.expiresInSeconds / 60));
			setPaired(session.token, 'Paired with the local connector. Session expires in about ' + minutes + ' minute(s).', 'success');
			// Session timing comes only from the connector: warn before expiry, end at expiry.
			const seconds = session.expiresInSeconds;
			if (seconds > 0) {
				const notice = Math.min(SESSION_EXPIRY_NOTICE_SECONDS, Math.floor(seconds / 2));
				const remaining = notice < 60 ? 'less than a minute' : 'about ' + Math.round(notice / 60) + ' minute(s)';
				timers.current.push(window.setTimeout(() => update({
					sessionStatus: status('Connector session ends in ' + remaining + '. ' + PAIR_AGAIN_GUIDANCE),
				}), (seconds - notice) * 1000));
				timers.current.push(window.setTimeout(() => setPaired(null, CONNECTOR_ERROR_MESSAGES.session_expired!, 'error'), seconds * 1000));
			}
			await loadTargets();
		} catch (error) {
			setPaired(null, connectorFailed(error, true) || 'Pairing failed.', 'error');
		} finally {
			update({ pairing: false });
		}
	}, [connectorFailed, loadTargets, setPaired, update]);

	const unpair = useCallback(async () => {
		const token = ref.current.sessionToken;
		setPaired(null, NOT_PAIRED, '');
		update({ rememberedTargetId: null }); // an explicit unpair forgets the target too
		if (!token) return;
		// The page's session is already gone; a failed unpair leaves one that expires on its own.
		try { await connector.unpair(token); } catch { /* nothing more to do */ }
	}, [setPaired, update]);

	// Any change of the login invalidates its check and the databases it listed. Typing
	// in Local Mode means local work: a remembered company target is forgotten.
	const setLogin = useCallback((change: { username?: string; password?: string }) => {
		const s = ref.current;
		if ((change.username ?? s.username) === s.username && (change.password ?? s.password) === s.password) return;
		update({
			...change, ...clearedDiscovery, busy: null,
			rememberedTargetId: s.target ? s.rememberedTargetId : null,
			connectionStatus: status(s.verified || s.databases ? 'Connection details changed; connect again.' : s.connectionStatus.text),
		});
	}, [update]);

	// Local: test the login, then list databases. Remote: discovery is the login check.
	const connect = useCallback(async () => {
		const s = ref.current;
		if (!s.username || !s.password) {
			update({ connectionStatus: status(s.target ? 'Enter your MySQL username and password for the remote target.'
				: 'Enter your MySQL username and password.', 'error') });
			return;
		}
		const binding = { target: s.target, username: s.username, password: s.password, token: s.sessionToken };
		// The connector session matters only for a company target; pairing during a Local
		// check does not cancel it.
		const same = () => {
			const now = ref.current;
			return now.target?.target_id === binding.target?.target_id && now.username === binding.username
				&& now.password === binding.password && (!binding.target || now.sessionToken === binding.token);
		};
		const previous = s.database;
		update({ ...clearedDiscovery, busy: binding.target ? 'discovering' : 'testing',
			connectionStatus: status(binding.target ? 'Reading visible database names…' : 'Testing connection…') });
		let list: DatabaseList;
		try {
			if (binding.target) {
				const data = await connector.discoverRemote(requireSession(), binding.target.target_id, binding);
				if (!same() || data.target_id !== binding.target.target_id)
					throw new RequestError('Target, login, or connector session changed during discovery. Discover again.');
				list = asDatabaseList(data);
			} else {
				await testLocalConnection(binding);
				if (!same()) return;
				update({ busy: 'discovering', connectionStatus: status('Login accepted. Reading visible database names…') });
				list = await discoverLocalDatabases(binding);
			}
		} catch (error) {
			// A late failure for an earlier target, login or session leaves the current state
			// alone (only a 401 for the session still in use ends it).
			if (!same()) {
				if (binding.target) connectorFailed(error, false, { token: binding.token, targetId: binding.target.target_id });
				return;
			}
			// A failed remote discovery also clears the connector's state for this session.
			update({ ...clearedDiscovery, busy: null,
				connectionStatus: status(binding.target ? connectorFailed(error) : errorText(error, 'Unable to connect to the database.'), 'error') });
			return;
		}
		if (!same()) return;
		update({
			busy: null, verified: true, databases: list.databases, listComplete: list.complete,
			database: previous && list.databases.includes(previous) ? previous : null,
			discovery: binding.target && binding.token ? {
				targetId: binding.target.target_id, username: binding.username, sessionToken: binding.token, databases: list.databases,
			} : null,
			connectionStatus: status('Login accepted by MySQL. ' + list.databases.length + ' database(s) discovered.', 'success'),
		});
	}, [connectorFailed, update]);

	// Only a discovered name can be chosen; typing a name is not possible.
	const selectDatabase = useCallback((name: string | null) => {
		const s = ref.current;
		update({ database: name && s.databases?.includes(name) ? name : null });
	}, [update]);

	// The page only asks: the connector shows the exact request and a one-time code in
	// its own window, and only that code approves it. No MySQL login is part of it.
	const requestTarget = useCallback(async (details: NewTarget | 'reapprove') => {
		update({ targetRequest: null, requestBusy: true });
		try {
			const token = requireSession();
			let body: connector.TargetRequestBody;
			if (details === 'reapprove') {
				const target = ref.current.target;
				if (!target) throw new RequestError('Select the target to re-approve.');
				body = { target_id: target.target_id };
			} else {
				body = { display_name: details.name.trim(), host: details.host.trim(), port: details.port, trust: details.trust };
				if (details.trust === 'bundle') {
					if (!details.caFile) throw new RequestError('Choose the CA bundle file (PEM certificates).');
					if (details.caFile.size > MAX_CA_BUNDLE_BYTES) throw new RequestError(CONNECTOR_ERROR_MESSAGES.ca_bundle_too_large!);
					body.ca_pem = await details.caFile.text();
				}
			}
			const requestId = await connector.requestTarget(token, body);
			update({ targetRequest: { requestId, sessionToken: token },
				requestStatus: status('Check the request in the connector window. If every line is what you asked for, enter its approval code here.') });
		} catch (error) {
			update({ requestStatus: status(connectorFailed(error) || 'The request was not accepted.', 'error') });
		} finally {
			update({ requestBusy: false });
		}
	}, [connectorFailed, update]);

	const confirmTarget = useCallback(async (code: string) => {
		const pending = ref.current.targetRequest;
		if (!pending || pending.sessionToken !== ref.current.sessionToken) {
			update({ targetRequest: null, requestStatus: status(CONNECTOR_ERROR_MESSAGES.target_request_not_found!, 'error') });
			return;
		}
		update({ requestBusy: true });
		try {
			const approvedId = await connector.confirmTarget(pending.sessionToken, pending.requestId, code.trim());
			update({ targetRequest: null, reapprovable: false });
			await loadTargets();
			if (ref.current.targets.some(t => t.target_id === approvedId)) {
				// Entering company mode: a login typed for Local is not sent to the new target.
				if (!ref.current.target) update({ username: '', password: '' });
				update({ rememberedTargetId: null });
				selectTarget(approvedId);
			}
			update({ requestStatus: status('Approved. Enter your MySQL username and password, then use Discover databases to test the connection.', 'success') });
		} catch (error) {
			if (error instanceof RequestError && error.code && CLOSES_REQUEST.includes(error.code)) update({ targetRequest: null });
			update({ requestStatus: status(connectorFailed(error) || 'The approval was not accepted.', 'error') });
		} finally {
			update({ requestBusy: false });
		}
	}, [connectorFailed, loadTargets, selectTarget, update]);

	// A company operation (validate, snapshot) with the selected target, the login and
	// the session; refused here, before anything is sent, when one of them is missing.
	const withTarget = useCallback(async <T,>(run: (token: string, target: connector.Target, login: connector.RemoteLogin,
		database: string | null) => Promise<T>, needDatabase: boolean): Promise<T> => {
		const s = ref.current;
		if (!s.target) throw new RequestError(REMOTE_NOT_AVAILABLE + 'Select an approved connector target instead of typing a remote host.');
		if (!s.username || !s.password) throw new RequestError('Enter your MySQL username and password for the remote target.');
		if ((s.database || needDatabase) && !discoveryMatches(s))
			throw new RequestError('The selected database was not discovered for this target, login and session. Discover again.');
		const token = requireSession();
		try {
			return await run(token, s.target, { username: s.username, password: s.password }, s.database);
		} catch (error) {
			throw new RequestError(connectorFailed(error, false, { token, targetId: s.target.target_id }));
		}
	}, [connectorFailed]);

	const actions = useMemo(() => ({
		selectTarget: (id: string) => { update({ rememberedTargetId: null }); selectTarget(id); },
		pair, unpair, loadTargets, setLogin, connect, selectDatabase, requestTarget, confirmTarget, withTarget,
		closeRequest: () => update({ targetRequest: null }),
		read: () => ref.current,
	}), [pair, unpair, loadTargets, setLogin, connect, selectDatabase, requestTarget, confirmTarget, withTarget, selectTarget, update]);

	return { state, actions };
}

export type ConnectionActions = ReturnType<typeof useConnection>['actions'];
