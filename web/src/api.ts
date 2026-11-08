// Typed access to the existing FastAPI endpoints. Responses are checked at runtime:
// a TypeScript type is a promise about the shape, not proof of it.

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type Confidence = 'HIGH' | 'LIMITED' | 'LOW';
const RISKS: readonly string[] = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
const CONFIDENCES: readonly string[] = ['HIGH', 'LIMITED', 'LOW'];

export interface Factor { label: string; points: number }

export interface StatementResult {
	index: number;
	sql: string;
	statementType: string;
	tables: string[];
	analysisStatus: string;
	parseError: string | null;
	score: number;
	riskLevel: RiskLevel | null;
	confidence: Confidence | null;
	factors: Factor[];
	reasons: string[];
	checklist: string[];
	evidenceStatus: string | null;
	evidenceMessage: string | null;
}

export interface Review {
	statementCount: number;
	overallScore: number;
	overallRisk: RiskLevel | null;
	overallConfidence: Confidence | null;
	analysisMode: string;
	statements: StatementResult[];
}

export interface Login { host: string; port: number; username: string; password: string }

export class ApiError extends Error { }

type Json = Record<string, unknown>;
const isObject = (value: unknown): value is Json => typeof value === 'object' && value !== null && !Array.isArray(value);
const str = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const num = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const strings = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []);
const risk = (value: unknown): RiskLevel | null => (typeof value === 'string' && RISKS.includes(value) ? value as RiskLevel : null);
const confidence = (value: unknown): Confidence | null =>
	(typeof value === 'string' && CONFIDENCES.includes(value) ? value as Confidence : null);

async function post(path: string, body: unknown, signal?: AbortSignal): Promise<Json> {
	let response: Response;
	try {
		response = await fetch(path, {
			method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
		});
	} catch (error) {
		if (error instanceof DOMException && error.name === 'AbortError') throw error;
		throw new ApiError('The validator did not respond. Is the application still running?');
	}
	let data: unknown = null;
	try { data = await response.json(); } catch { /* handled below */ }
	if (!isObject(data)) throw new ApiError(`Unexpected response (HTTP ${response.status}).`);
	if (!response.ok || data.status === 'error') throw new ApiError(errorText(data, response.status));
	return data;
}

function errorText(data: Json, status: number): string {
	const message = str(data.message) ?? (isObject(data.detail) ? str(data.detail.message) : str(data.detail));
	return message ?? `The request failed (HTTP ${status}).`;
}

function statement(value: unknown): StatementResult | null {
	if (!isObject(value)) return null;
	const evidence = isObject(value.database_evidence) ? value.database_evidence : {};
	return {
		index: num(value.index) ?? 0,
		sql: str(value.sql) ?? '',
		statementType: str(value.statement_type) ?? 'UNKNOWN',
		tables: strings(value.tables),
		analysisStatus: str(value.analysis_status) ?? 'unknown',
		parseError: str(value.parse_error),
		score: num(value.score) ?? 0,
		riskLevel: risk(value.risk_level),
		confidence: confidence(value.confidence),
		factors: Array.isArray(value.factors)
			? value.factors.flatMap(f => (isObject(f) && str(f.label) !== null ? [{ label: f.label as string, points: num(f.points) ?? 0 }] : []))
			: [],
		reasons: strings(value.reasons),
		checklist: strings(value.checklist),
		evidenceStatus: str(evidence.status),
		evidenceMessage: str(evidence.message),
	};
}

export async function validate(sql: string, login: Login | null, database: string | null, signal: AbortSignal): Promise<Review> {
	const body = login && database ? { sql, ...login, database } : { sql };
	const data = await post('/api/validate', body, signal);
	const statements = Array.isArray(data.statements) ? data.statements.map(statement) : [];
	if (statements.some(s => s === null) || num(data.overall_score) === null) throw new ApiError('Unexpected review response.');
	return {
		statementCount: num(data.statement_count) ?? statements.length,
		overallScore: num(data.overall_score) ?? 0,
		overallRisk: risk(data.overall_risk_level),
		overallConfidence: confidence(data.overall_confidence),
		analysisMode: str(data.analysis_mode) ?? 'STATIC',
		statements: statements as StatementResult[],
	};
}

export async function testConnection(login: Login): Promise<void> {
	const data = await post('/api/connections/test', login);
	// Connected only when the server says so: status success and connected true.
	if (data.status !== 'success' || data.connected !== true) throw new ApiError(errorText(data, 200));
}

export interface Discovery { databases: string[]; complete: boolean }

export async function discoverDatabases(login: Login): Promise<Discovery> {
	const data = await post('/api/connections/discover-databases', login);
	if ((data.status !== 'success' && data.status !== 'limited') || !Array.isArray(data.databases))
		throw new ApiError(errorText(data, 200));
	return { databases: strings(data.databases), complete: data.status === 'success' && data.complete !== false };
}
