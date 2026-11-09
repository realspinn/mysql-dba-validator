// A snapshot read only when the DBA asks, bound to the current target, login and
// database. Any change of those (even back to an earlier one) starts a new generation
// and discards a read still in flight; a shown result is dropped as soon as the context
// changes. Memory only, never stored.
import { useEffect, useRef, useState } from 'react';
import * as connector from '../../api/connector';
import { localSnapshot } from '../../api/local';
import { RequestError, errorText, type Json } from '../../api/http';
import { contextKey, discoveryMatches, usableDatabase, type ConnectionState } from '../connection/connectionState';
import type { ConnectionActions } from '../connection/useConnection';
import type { SnapshotKind } from './snapshotText';

export type SnapshotResult = { kind: 'data'; data: Json } | { kind: 'failure'; message: string };

// Why nothing can be loaded yet, or null when a snapshot can be requested.
export function snapshotBlocker(kind: SnapshotKind, s: ConnectionState): string | null {
	if (s.target) {
		if (!s.database || !discoveryMatches(s))
			return kind.subject + ' is read for one database. Enter your MySQL login for ' + connector.targetName(s.target)
				+ ' and discover and select a database in the connection settings.';
		return null;
	}
	if (!usableDatabase(s))
		return kind.subject + ' is read for one database. Enter your MySQL login and choose a database in the SQL review connection settings.';
	return null;
}

export function useSnapshot(kind: SnapshotKind, state: ConnectionState, actions: ConnectionActions) {
	const key = contextKey(state);
	const [result, setResult] = useState<{ key: string; value: SnapshotResult } | null>(null);
	const [loading, setLoading] = useState(false);
	const generation = useRef(0);
	const seen = useRef(key);
	if (seen.current !== key) {
		seen.current = key;
		generation.current += 1;
	}
	// The target, login or database changed: the last result no longer describes it.
	const shown = result && result.key === key ? result.value : null;
	useEffect(() => { if (result && result.key !== key) setResult(null); }, [key, result]);

	const load = async () => {
		const s = actions.read();
		if (loading || snapshotBlocker(kind, s)) return;
		const started = generation.current;
		const startedKey = contextKey(s);
		setLoading(true);
		let value: SnapshotResult;
		try {
			const database = usableDatabase(s)!;
			const data = s.target
				? await actions.withTarget((token, target, login) => connector.remoteSnapshot(token, target.target_id, kind.route, login, database), true)
				: await localSnapshot(kind.route, s, database);
			if (typeof data.status !== 'string' || !Array.isArray(data[kind.list]))
				throw new RequestError('The diagnostic returned an unexpected response.');
			value = { kind: 'data', data };
		} catch (error) {
			value = { kind: 'failure', message: errorText(error, 'The request did not complete.') };
		}
		setLoading(false);
		// The context changed while reading (even if it changed back): discard.
		if (started !== generation.current) return;
		setResult({ key: startedKey, value });
	};

	return { result: shown, loading, load, blocker: snapshotBlocker(kind, state) };
}
