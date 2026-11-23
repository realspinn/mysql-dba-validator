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
	// A new or stale result is not what the last message was about.
	useEffect(() => { setNote(null); }, [summary]);
	useEffect(() => () => window.clearTimeout(timer.current), []);

	const copy = async () => {
		if (summary === null) return;
		const ok = await writeToClipboard(navigator.clipboard, summary);
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
