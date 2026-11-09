// Observed performance (/api/diagnostics/observed-performance) and lock waits
// (/api/diagnostics/lock-waits), as the server sends them.

export interface Family {
	digest: string;
	digest_text?: string | null;
	digest_text_truncated?: boolean;
	executions?: number;
	total_time_ms?: number | null;
	avg_time_ms?: number | null;
	max_time_ms?: number | null;
	p95_time_ms?: number | null;
	lock_time_ms?: number | null;
	rows_examined?: number;
	rows_sent?: number;
	rows_affected?: number;
	no_index_used?: number;
	first_seen?: string;
	last_seen?: string;
	timing?: string;
}

export interface PerfSnapshot {
	status: string;
	observed_at?: string;
	schema?: string;
	families: Family[];
	more_families?: boolean;
	limit?: number;
	digest_consumer?: string;
	overflow_executions?: number;
}

export interface LockSide { session_id?: number | null; thread_id?: number; seconds_in_state?: number | null }

export interface LockWait {
	waiting: LockSide;
	blocking: LockSide;
	table: string;
	partition?: string | null;
	index?: string | null;
	lock_type?: string;
	requested_mode?: string;
	blocking_mode?: string;
}

export interface LockSnapshot {
	status: string;
	observed_at?: string;
	schema?: string;
	waits: LockWait[];
	more_waits?: boolean;
	limit?: number;
	sessions?: string;
}

export type Snapshot = PerfSnapshot | LockSnapshot;
