// The SQL being reviewed and the last result. Kept for the whole page (the dashboard
// shows it too); memory only, never stored.
import { useCallback, useRef, useState } from 'react';
import { asReview, validateLocal } from '../../api/local';
import * as connector from '../../api/connector';
import { errorText } from '../../api/http';
import { PUBLIC_DEMO } from '../../shared/environment';
import type { ReviewResponse } from '../../types/review';
import { usableDatabase, type ConnectionState } from '../connection/connectionState';
import type { ConnectionActions } from '../connection/useConnection';

export type Run = { status: 'idle' } | { status: 'running' } | { status: 'failed'; message: string };
export interface Reviewed { result: ReviewResponse; sql: string; context: string; at: Date }

// What a result was produced with, to say when it no longer matches the page. A company
// review always sends the login, so any edit of it (a password too, counted, never kept)
// makes its result stale; a Local one sends it only once it is verified.
export const reviewContext = (s: ConnectionState) =>
	PUBLIC_DEMO ? 'demo' : [s.target?.target_id ?? 'local', s.target || s.verified ? s.username : '',
		s.target ? s.loginRevision : '', usableDatabase(s) ?? ''].join('|');

export function useReview(connection: ConnectionActions) {
	const [sql, setSql] = useState('');
	const [run, setRun] = useState<Run>({ status: 'idle' });
	const [reviewed, setReviewed] = useState<Reviewed | null>(null);
	const abort = useRef<AbortController | null>(null);

	const start = useCallback(async (text: string) => {
		if (!text.trim()) return;
		abort.current?.abort();
		const controller = new AbortController();
		abort.current = controller;
		setRun({ status: 'running' });
		const s = connection.read();
		const context = reviewContext(s);
		try {
			// A selected company target takes the connector path only; the online demo never does.
			const result = !PUBLIC_DEMO && s.target
				? asReview(await connection.withTarget((token, target, login, database) =>
					connector.validateRemote(token, target.target_id, text, login, database), false))
				: await validateLocal(text, s.verified ? s : null, usableDatabase(s), controller.signal);
			if (controller.signal.aborted) return;
			setReviewed({ result, sql: text, context, at: new Date() });
			setRun({ status: 'idle' });
		} catch (error) {
			if (controller.signal.aborted) return;
			setRun({ status: 'failed', message: errorText(error, 'Validation failed.') });
		}
	}, [connection]);

	return { sql, setSql, run, reviewed, start };
}

export type ReviewStore = ReturnType<typeof useReview>;
