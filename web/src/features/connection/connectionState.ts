// Where SQL is reviewed and with which evidence: Local (MySQL on this computer) or an
// approved company target through the local connector. Memory only: nothing here is
// ever stored, and the password and session token never leave this state except in
// the body (password) or Authorization header (token) of the request that needs them.
import type { Target } from '../../api/connector';
import { status, type Status } from '../../shared/status';
export { status, type Status };

// Which target, MySQL username and connector session produced the current remote
// database list. A database is sent with remote work only while all three still match;
// the connector re-checks the same binding itself.
export interface Discovery { targetId: string; username: string; sessionToken: string; databases: string[] }

export interface ConnectionState {
	target: Target | null;          // null: Local
	username: string;
	password: string;
	verified: boolean;              // the current login was accepted (Local test, or remote discovery)
	busy: 'testing' | 'discovering' | null;
	databases: string[] | null;     // null: not discovered
	listComplete: boolean;
	database: string | null;
	discovery: Discovery | null;
	connectionStatus: Status;

	sessionToken: string | null;
	sessionStatus: Status;
	targets: Target[];
	rememberedTargetId: string | null;
	pairing: boolean;

	targetRequest: { requestId: string; sessionToken: string } | null;
	requestStatus: Status;
	requestBusy: boolean;
	reapprovable: boolean;
}

export const NOT_PAIRED = 'Not paired. Remote targets need a pairing code from the local connector.';
export const LOCAL_START = 'Optional. Adds execution-plan and table metadata evidence. The login is never saved.';

export const initialConnection = (): ConnectionState => ({
	target: null, username: '', password: '', verified: false, busy: null, databases: null, listComplete: true,
	database: null, discovery: null, connectionStatus: status(LOCAL_START),
	sessionToken: null, sessionStatus: status(NOT_PAIRED), targets: [], rememberedTargetId: null, pairing: false,
	targetRequest: null, requestStatus: status(''), requestBusy: false, reapprovable: false,
});

// Drops the login check, every discovered database and the selection.
export const clearedDiscovery: Partial<ConnectionState> = {
	verified: false, databases: null, listComplete: true, database: null, discovery: null,
};

// True only while the selected database came from discovery for this exact target,
// MySQL username and connector session.
export function discoveryMatches(s: ConnectionState): boolean {
	const d = s.discovery;
	return Boolean(d && s.target && s.database && d.targetId === s.target.target_id
		&& d.username === s.username && d.sessionToken === s.sessionToken && d.databases.includes(s.database));
}

// The database a review or snapshot may use now, or null.
export function usableDatabase(s: ConnectionState): string | null {
	if (!s.verified || !s.database) return null;
	if (s.target) return discoveryMatches(s) ? s.database : null;
	return s.database;
}

// What a snapshot was read for. The password is never part of it.
export const contextKey = (s: ConnectionState) =>
	[s.target ? 'remote:' + s.target.target_id : 'local', s.username, usableDatabase(s) ?? ''].join('|');
