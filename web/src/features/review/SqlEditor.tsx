// The SQL to review: a plain textarea with line numbers. Ctrl/Cmd+Enter runs the review.
import { useRef, useState } from 'react';

interface Props {
	value: string;
	onChange: (value: string) => void;
	onRun: () => void;
	running: boolean;
	note: string;
	textareaRef?: React.Ref<HTMLTextAreaElement>;
}

export function SqlEditor({ value, onChange, onRun, running, note, textareaRef }: Props) {
	const [copied, setCopied] = useState(false);
	const gutter = useRef<HTMLDivElement>(null);
	const lines = value.split('\n').length;

	const copy = async () => {
		try {
			await navigator.clipboard.writeText(value);
			setCopied(true);
			window.setTimeout(() => setCopied(false), 1400);
		} catch { /* clipboard not allowed: nothing copied */ }
	};

	return (
		<div className="panel editor-panel">
			<div className="toolbar">
				<span className="toolbar-title" id="sqlLabel">SQL</span>
				<span className="toolbar-meta">{lines} {lines === 1 ? 'line' : 'lines'} · {value.length.toLocaleString()} characters</span>
				<span className="spacer" />
				<button type="button" className="btn btn-quiet btn-sm" onClick={() => void copy()} disabled={!value}>{copied ? 'Copied' : 'Copy'}</button>
				<button type="button" className="btn btn-quiet btn-sm" onClick={() => onChange('')} disabled={!value}>Clear</button>
			</div>
			<div className="editor">
				<div className="gutter" ref={gutter} aria-hidden="true">
					{Array.from({ length: lines }, (_, i) => <span key={i}>{i + 1}</span>)}
				</div>
				<textarea id="sqlInput" ref={textareaRef} aria-labelledby="sqlLabel" spellCheck={false} value={value}
					placeholder={"UPDATE orders SET status = 'archived'\nWHERE created_at < '2025-01-01';"}
					onChange={e => onChange(e.target.value)}
					onScroll={e => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop; }}
					onKeyDown={e => {
						if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!running) onRun(); }
					}} />
			</div>
			<div className="editor-actions">
				<button type="button" className="btn btn-primary" onClick={onRun} disabled={!value.trim() || running} aria-busy={running}>
					{running ? 'Reviewing…' : 'Review SQL'}
				</button>
				<span className="hint"><kbd>Ctrl</kbd><kbd>Enter</kbd></span>
				<span className="spacer" />
				<span className="mode-note">{note}</span>
			</div>
		</div>
	);
}
