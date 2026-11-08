// The local connector at 127.0.0.1:8765: the only way the page reaches a company
// (remote) target. Remote work never goes to the public /api/* routes. A target is
// referred to only by its approved target_id; the MySQL login travels in the body of
// each operation and the connector does not store it. The session token comes only
// from explicit pairing and is held in memory by the caller, never stored.
import { CONNECTOR_ERROR_MESSAGES } from './messages';
import { RequestError, requestJson, type Json } from './http';

export const CONNECTOR_URL = 'http://127.0.0.1:8765';

export interface Target {
	target_id: string;
	display_name?: string;
	host: string;
	port: number;
	approval_state: string;
}

export const targetName = (target: Target) => String(target.display_name || target.target_id);

// Any non-empty host that is not an explicit loopback name is remote.
export function isRemoteHost(hostname: string): boolean {
	const normalized = hostname.trim().toLowerCase();
	return normalized !== '' && normalized !== 'localhost' && normalized !== '127.0.0.1' && normalized !== '::1';
}

const auth = (token: string) => ({ Authorization: 'Bearer ' + token });

// Every connector call goes through here. Errors are re-thrown with the connector's
// stable code and a fixed message, never retried against the public API.
async function connectorRequest(path: string, method: 'GET' | 'POST', token: string | null, body?: unknown): Promise<Json> {
	const url = CONNECTOR_URL + path;
	if (!url.startsWith(CONNECTOR_URL + '/')) throw new RequestError('Refusing a non-connector request.');
	try {
		return await requestJson(url, { method, headers: token ? auth(token) : {}, body }, CONNECTOR_ERROR_MESSAGES,
			'The local connector is not reachable at ' + CONNECTOR_URL + '. Is it running?');
	} catch (error) {
		if (!(error instanceof RequestError) || error.status === null) throw error;
		const friendly = error.code !== null && Object.hasOwn(CONNECTOR_ERROR_MESSAGES, error.code) ? CONNECTOR_ERROR_MESSAGES[error.code] : undefined;
		throw new RequestError(friendly ?? error.message, error.status, error.code);
	}
}

const targetPath = (targetId: string, action: string) => '/company/targets/' + encodeURIComponent(targetId) + '/' + action;

export interface Pairing { token: string; expiresInSeconds: number }

export async function pair(code: string): Promise<Pairing> {
	const data = await connectorRequest('/pair', 'POST', null, { pairing_token: code });
	if (data.status !== 'paired' || typeof data.session_token !== 'string' || !data.session_token)
		throw new RequestError('The local connector returned an unexpected pairing response.');
	return { token: data.session_token, expiresInSeconds: Number(data.expires_in_seconds) || 0 };
}

export async function unpair(token: string): Promise<void> {
	await connectorRequest('/unpair', 'POST', token);
}

// Approved targets only, and only those that really are remote.
export async function approvedTargets(token: string): Promise<Target[]> {
	const data = await connectorRequest('/company/targets', 'GET', token);
	const targets: unknown[] = Array.isArray(data.targets) ? data.targets : [];
	return targets.filter((t): t is Target => typeof t === 'object' && t !== null
		&& (t as Target).approval_state === 'approved' && typeof (t as Target).target_id === 'string'
		&& (t as Target).target_id !== '' && isRemoteHost(String((t as Target).host || '')));
}

export type TargetRequestBody =
	| { target_id: string }
	| { display_name: string; host: string; port: number; trust: string; ca_pem?: string };

export async function requestTarget(token: string, body: TargetRequestBody): Promise<string> {
	const data = await connectorRequest('/company/target-requests', 'POST', token, body);
	if (data.status !== 'awaiting_approval' || typeof data.request_id !== 'string')
		throw new RequestError('The local connector returned an unexpected response.');
	return data.request_id;
}

// The approval code goes in the request body only, never in a URL.
export async function confirmTarget(token: string, requestId: string, code: string): Promise<string> {
	const data = await connectorRequest('/company/target-requests/confirm', 'POST', token,
		{ request_id: requestId, approval_code: code });
	const target = data.target;
	return typeof target === 'object' && target !== null && typeof (target as Target).target_id === 'string'
		? (target as Target).target_id : '';
}

export interface RemoteLogin { username: string; password: string }

// Fixed-purpose discovery: the body carries only the per-request login.
export async function discoverRemote(token: string, targetId: string, login: RemoteLogin): Promise<Json> {
	return connectorRequest(targetPath(targetId, 'databases'), 'POST', token, { username: login.username.trim(), password: login.password });
}

export async function validateRemote(token: string, targetId: string, sql: string, login: RemoteLogin,
	database: string | null): Promise<Json> {
	const body: Json = { sql, username: login.username.trim(), password: login.password };
	if (database) body.database = database;
	return connectorRequest(targetPath(targetId, 'validate'), 'POST', token, body);
}

export async function remoteSnapshot(token: string, targetId: string, route: string, login: RemoteLogin,
	database: string): Promise<Json> {
	return connectorRequest(targetPath(targetId, route), 'POST', token,
		{ username: login.username.trim(), password: login.password, database });
}
