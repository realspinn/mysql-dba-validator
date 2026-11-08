// JSON requests. Errors carry the HTTP status and the server's stable error code, so a
// caller can map them to a fixed message; no raw server text is invented here.

export type Json = Record<string, unknown>;

export const isObject = (value: unknown): value is Json =>
	typeof value === 'object' && value !== null && !Array.isArray(value);

export class RequestError extends Error {
	readonly status: number | null;
	readonly code: string | null;
	constructor(message: string, status: number | null = null, code: string | null = null) {
		super(message);
		this.status = status;
		this.code = code;
	}
}

// The message a response carries, else the fixed message for its error code.
export function responseMessage(data: unknown, messages: Readonly<Record<string, string>>, fallback: string): string {
	if (!isObject(data)) return fallback;
	if (typeof data.message === 'string') return data.message;
	const code = typeof data.error_code === 'string' ? data.error_code : typeof data.error === 'string' ? data.error : null;
	return (code !== null && Object.hasOwn(messages, code) ? messages[code] : undefined) ?? fallback;
}

export interface RequestOptions {
	method?: 'GET' | 'POST';
	headers?: Record<string, string>;
	body?: unknown;
	signal?: AbortSignal;
}

// status null: no response at all (server not running, or the request was blocked).
export async function requestJson(url: string, options: RequestOptions,
	messages: Readonly<Record<string, string>>, fallback: string): Promise<Json> {
	const headers: Record<string, string> = { ...options.headers };
	if (options.body !== undefined) headers['Content-Type'] = 'application/json';
	let response: Response;
	try {
		response = await fetch(url, {
			method: options.method ?? 'POST',
			headers,
			body: options.body === undefined ? undefined : JSON.stringify(options.body),
			signal: options.signal,
			cache: 'no-store',
		});
	} catch (error) {
		if (error instanceof DOMException && error.name === 'AbortError') throw error;
		throw new RequestError(fallback);
	}
	let data: unknown = null;
	try { data = await response.json(); } catch { data = null; }
	if (!response.ok) {
		const code = isObject(data) && typeof data.error_code === 'string' ? data.error_code : null;
		throw new RequestError(responseMessage(data, messages, 'The server could not complete this request.'), response.status, code);
	}
	if (!isObject(data)) throw new RequestError('The server returned an unexpected response.', response.status);
	return data;
}

export const errorText = (error: unknown, fallback: string): string =>
	error instanceof Error && error.message ? error.message : fallback;
