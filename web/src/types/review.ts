// The /api/validate (and connector /validate) response, as the server sends it. Fields
// the server may omit are optional; the review model treats a missing field as
// "not reported", never as a value.

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type Confidence = 'HIGH' | 'LIMITED' | 'LOW';

export interface Factor {
	label?: string;
	text?: string;
	code?: string;
	points?: number;
}

export interface EvidenceState {
	connection: { state: string; reason?: string | null };
	plan: { state: string; reason?: string | null; interpreted?: boolean; rows?: number };
	metadata: { state: string; reason?: string | null; tables?: { name?: string; found?: boolean | null }[] };
	overall: string;
}

export interface DatabaseEvidence {
	status?: string;
	message?: string;
	access_type?: string | null;
	estimated_rows?: number | null;
	possible_keys?: string[] | null;
	key?: string | null;
	key_length?: string | number | null;
	full_table_scan?: boolean | null;
	extra?: string | null;
}

export interface Scope {
	basis?: string;
	table?: string;
	index?: string;
	indirect_effects?: string;
}

export interface FilterIndex {
	state?: string;
	table?: string;
	column?: string;
}

export interface OptimizationItem {
	title?: string;
	observation?: string;
	action?: string;
	reasoning?: string;
	caveats?: string[];
	verify?: string;
}

export interface Optimization {
	status?: string;
	reason?: string;
	recommendations?: OptimizationItem[];
}

export interface Statement {
	index?: number;
	sql: string;
	statement_type?: string;
	tables?: string[];
	analysis_status?: string;
	analysis_text_differs?: boolean;
	parse_error?: string | null;
	score?: number;
	static_score?: number;
	risk_level?: RiskLevel | null;
	static_risk_level?: RiskLevel | null;
	confidence?: Confidence | null;
	factors?: Factor[];
	evidence_factors?: Factor[];
	metadata_factors?: Factor[];
	metadata_score_adjustment?: number;
	high_estimated_rows_applied?: boolean | null;
	reasons?: string[];
	checklist?: (string | { text?: string; name?: string })[];
	evidence?: EvidenceState;
	database_evidence?: DatabaseEvidence;
	scope?: Scope | null;
	filter_index?: FilterIndex;
	has_limit?: boolean;
	optimization?: Optimization | null;
}

export interface EvidenceSummary {
	connection: { state: string };
	plan?: Record<string, number>;
	metadata?: Record<string, number>;
	partial?: boolean;
}

export interface ReviewResponse {
	statement_count?: number;
	overall_score?: number;
	overall_risk_level?: RiskLevel | null;
	overall_confidence?: Confidence | null;
	analysis_mode?: string;
	evidence_summary?: EvidenceSummary;
	statements: Statement[];
}
