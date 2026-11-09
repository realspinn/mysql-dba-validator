// The application's own server (/api/*). Local Mode only: MySQL on this computer at
// 127.0.0.1:3306. A company (remote) target is never sent here; see connector.ts.
import { LOCAL_HOST, LOCAL_PORT, PUBLIC_DEMO } from '../shared/environment';
import { RESPONSE_ERROR_MESSAGES } from './messages';
import { RequestError, requestJson, responseMessage, type Json } from './http';
import type { ReviewResponse } from '../types/review';

export interface LocalLogin { username: string; password: string }

const server = (login: LocalLogin) => ({ host: LOCAL_HOST, port: LOCAL_PORT, username: login.username.trim(), password: login.password });
const post = (path: string, body: unknown, fallback: string, signal?: AbortSignal) =>
	requestJson(path, { body, signal }, RESPONSE_ERROR_MESSAGES, fallback);

export function asReview(data: Json): ReviewResponse {
	if (!Array.isArray(data.statements))
		throw new RequestError(responseMessage(data, RESPONSE_ERROR_MESSAGES, 'Validation returned an unexpected response.'));
	return data as unknown as ReviewResponse;
}

// The online demo sends the SQL text only. Otherwise a verified login (and a chosen
// database) adds database evidence; without one the review is static.
export async function validateLocal(sql: string, login: LocalLogin | null, database: string | null,
	signal: AbortSignal): Promise<ReviewResponse> {
	const body: Json = PUBLIC_DEMO || !login ? { sql } : { sql, ...server(login) };
	if (!PUBLIC_DEMO && login && database) body.database = database;
	return asReview(await post('/api/validate', body, 'The validator did not respond. Is the application still running?', signal));
}

export async function testLocalConnection(login: LocalLogin): Promise<void> {
	const data = await post('/api/connections/test', server(login), 'Unable to connect to the database.');
	// Connected only when the server says so.
	if (data.status !== 'success' || data.connected !== true)
		throw new RequestError(responseMessage(data, RESPONSE_ERROR_MESSAGES, 'Unable to connect to the database.'));
}

export interface DatabaseList { databases: string[]; complete: boolean }

export function asDatabaseList(data: Json): DatabaseList {
	if (!Array.isArray(data.databases) || (data.status !== 'success' && data.status !== 'limited'))
		throw new RequestError(responseMessage(data, RESPONSE_ERROR_MESSAGES, 'Unable to discover databases.'));
	return {
		databases: data.databases.map(String),
		complete: data.status === 'success' && data.complete !== false,
	};
}

export async function discoverLocalDatabases(login: LocalLogin): Promise<DatabaseList> {
	return asDatabaseList(await post('/api/connections/discover-databases', server(login), 'Unable to discover databases.'));
}

// Observed performance and lock waits (fixed, read-only Performance Schema queries).
export async function localSnapshot(route: string, login: LocalLogin, database: string): Promise<Json> {
	const data = await post('/api/diagnostics/' + route, { ...server(login), database }, 'The request did not complete.');
	// Refusals before any connection come back as {error_code, message}.
	if (typeof data.error_code === 'string' && typeof data.status !== 'string')
		throw new RequestError(responseMessage(data, RESPONSE_ERROR_MESSAGES, 'The request was refused.'));
	return data;
}

// The page is still open: keeps the windowed desktop app from idling out. No data.
export function sendHeartbeat(): void {
	fetch('/api/heartbeat', { method: 'POST', cache: 'no-store' }).catch(() => { /* app not running */ });
}

// Update check and in-app download (Windows). The page only asks its own server.
export async function postUpdate(action: 'status' | 'download' | 'progress' | 'cancel' | 'show'): Promise<Json | null> {
	try {
		const response = await fetch('/api/update-' + action, { method: 'POST', cache: 'no-store' });
		if (!(response.ok || response.status === 409)) return null;
		if (response.status === 204) return {};
		const data: unknown = await response.json();
		return typeof data === 'object' && data !== null && !Array.isArray(data) ? data as Json : null;
	} catch {
		return null;
	}
}
