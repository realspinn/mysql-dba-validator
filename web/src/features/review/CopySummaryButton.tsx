// "Copy review summary": copies the text of copySummary.ts, only on a click, only for
// the result that is current. Nothing is sent anywhere; the text goes to the clipboard.
import { useEffect, useRef, useState } from 'react';
import { writeToClipboard } from './copySummary';

const MESSAGES = {
	copied: 'Summary copied.',
	failed: 'The browser did not allow copying. Check the clipboard permission for this page and try again.',
};

// `summary` is null when the result is stale (copySummaryFor): the button is then off and
// the notice above the result says to review again.
export function CopySummary({ summary }: { summary: string | null }) {
	const [note, setNote] = useState<keyof typeof MESSAGES | null>(null);
	const timer = useRef(0);
	// The summary this component shows now, or null once it is replaced, stale or gone. A
	// clipboard write is asynchronous: when it finishes, its message is shown only if the
	// result it copied is still the current one.
	const current = useRef<string | null>(null);
	useEffect(() => {
		current.current = summary;
		setNote(null);
		return () => { current.current = null; window.clearTimeout(timer.current); };
	}, [summary]);

	const copy = async () => {
		if (summary === null) return;
		const ok = await writeToClipboard(navigator.clipboard, summary);
		if (current.current !== summary) return;
		setNote(ok ? 'copied' : 'failed');
		window.clearTimeout(timer.current);
		timer.current = window.setTimeout(() => setNote(null), 5000);
	};

	return (
		<div className="summary-copy">
			<button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy()} disabled={summary === null}>
				Copy review summary
			</button>
			<span className={'summary-note' + (note === 'failed' ? ' summary-note-error' : '')} role="status">{note ? MESSAGES[note] : ''}</span>
		</div>
	);
}
