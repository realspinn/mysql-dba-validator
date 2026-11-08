// Update notice and, in the packaged Windows app, the in-app download. The ring and bar
// show the bytes actually received; nothing is installed or run.
import { useCallback, useEffect, useRef, useState } from 'react';
import { postUpdate } from '../api/local';
import type { Json } from '../api/http';
import { Icon } from '../shared/Icon';
import {
	confirmUpdated, downloadOffer, megabytes, readDismissed, RELEASE_PAGE, saveDismissed, savePending,
	UPDATE_VERSION, type Offer,
} from './updates';

const POLL_MS = 400;

interface Stage {
	stage: 'available' | 'downloading' | 'verifying' | 'ready' | 'failed' | 'updated';
	title: string;
	text: string;
	detail: string;
	progress: number | null; // fraction of real bytes, or null for "not measured"
}

export function UpdateNotice() {
	const [offer, setOffer] = useState<Offer | null>(null);
	const [stage, setStage] = useState<Stage | null>(null);
	const [hidden, setHidden] = useState(true);
	const [starting, setStarting] = useState(false);
	const dismissedHere = useRef('');
	const timer = useRef(0);
	const offerRef = useRef<Offer | null>(null);
	const stageRef = useRef<Stage | null>(null);
	offerRef.current = offer;
	stageRef.current = stage;

	const showOffer = useCallback((o: Offer, note = '') => {
		const d = o.download;
		const detail = d ? d.platform + ' · ' + megabytes(d.size) + (d.inApp ? '' : ' · downloaded by your browser')
			: 'Download it from the release notes page.';
		setStage({ stage: 'available', title: 'Update available', progress: 0, detail: note || detail,
			text: 'MySQL DBA Validator ' + o.version + (o.current ? ' · you have ' + o.current : '') });
		setHidden(false);
	}, []);

	const later = () => {
		if (offerRef.current) { dismissedHere.current = offerRef.current.version; saveDismissed(offerRef.current.version); }
		setHidden(true);
	};

	// Shows the backend's download state and keeps polling while it is under way: one
	// timer at a time, and a failed poll during a download is tried again.
	const follow = useCallback((snapshot: Json | null) => {
		window.clearTimeout(timer.current);
		const o = offerRef.current;
		if (!o) return;
		const poll = () => { timer.current = window.setTimeout(() => void postUpdate('progress').then(follow), POLL_MS); };
		if (!snapshot) {
			const now = stageRef.current?.stage;
			if (now === 'downloading' || now === 'verifying') poll();
			return;
		}
		// Idle carries no version: the app restarted (or cancelled) during a download.
		if (snapshot.stage === 'idle') {
			const now = stageRef.current?.stage;
			if (now === 'downloading' || now === 'verifying') showOffer(o, 'The download stopped. Nothing was changed. Try again.');
			return;
		}
		if (snapshot.version !== o.version) return;
		const received = Number.isSafeInteger(snapshot.received) ? snapshot.received as number : 0;
		const total = Number.isSafeInteger(snapshot.total) && (snapshot.total as number) > 0 ? snapshot.total as number : 0;
		const text = 'MySQL DBA Validator ' + o.version;
		if (snapshot.stage === 'downloading') {
			const fraction = total ? Math.min(1, received / total) : null;
			setStage({ stage: 'downloading', title: 'Downloading update', text, progress: fraction,
				detail: total ? megabytes(received) + ' of ' + megabytes(total) + ' · ' + Math.floor((fraction ?? 0) * 100) + '%' : megabytes(received) });
			poll();
		} else if (snapshot.stage === 'verifying') {
			setStage({ stage: 'verifying', title: 'Verifying download', text, progress: null,
				detail: 'Checking the size, the SHA-256 digest from GitHub and the archive contents.' });
			poll();
		} else if (snapshot.stage === 'ready') {
			savePending(o.version);
			setStage({ stage: 'ready', title: 'Ready to install', text: text + ' · verified', progress: 1,
				detail: 'Close MySQL DBA Validator, extract the ZIP, then open MySQL-DBA-Validator.exe in the new folder. Your saved settings are kept.' });
		} else if (snapshot.stage === 'failed') {
			const message = typeof snapshot.message === 'string' && snapshot.message.length <= 200 ? snapshot.message : 'The update could not be downloaded.';
			setStage({ stage: 'failed', title: 'Update not downloaded', text, progress: 0, detail: message });
		} else if (snapshot.stage === 'cancelled') {
			showOffer(o, 'Download cancelled. Nothing was changed.');
		}
	}, [showOffer]);

	const startDownload = async () => {
		setStarting(true);
		const snapshot = await postUpdate('download');
		setStarting(false);
		if (snapshot && snapshot.error === 'update_download_running') follow(await postUpdate('progress'));
		else if (snapshot && typeof snapshot.stage === 'string') follow(snapshot);
		else if (offerRef.current) showOffer(offerRef.current, 'The download could not be started. Try again.');
	};

	useEffect(() => {
		let active = true;
		void postUpdate('status').then(data => {
			if (!active || !data) return;
			const current = typeof data.current === 'string' && UPDATE_VERSION.test(data.current) ? data.current : '';
			if (confirmUpdated(current)) {
				setOffer({ version: current, current, download: null });
				setStage({ stage: 'updated', title: 'Update complete', text: 'MySQL DBA Validator ' + current + ' is running.', detail: '', progress: 1 });
				setHidden(false);
				return;
			}
			const version = data.state === 'update_available' && typeof data.version === 'string' && UPDATE_VERSION.test(data.version) ? data.version : '';
			if (!version || data.url !== RELEASE_PAGE + version || version === dismissedHere.current || version === readDismissed()) return;
			const o: Offer = { version, current, download: downloadOffer(data.download, version) };
			offerRef.current = o;
			setOffer(o);
			showOffer(o);
			// A download started earlier (or in another tab) carries on where it is.
			if (o.download?.inApp) void postUpdate('progress').then(follow);
		});
		return () => { active = false; window.clearTimeout(timer.current); };
	}, [follow, showOffer]);

	if (hidden || !stage || !offer) return null;
	const percent = stage.progress === null ? 0 : Math.min(100, Math.max(0, stage.progress * 100));
	const busy = stage.stage === 'downloading' || stage.stage === 'verifying';
	const cancel = () => void postUpdate('cancel').then(follow);
	const glyph = stage.stage === 'ready' || stage.stage === 'updated' ? 'check' : stage.stage === 'failed' ? 'alert' : 'arrowDown';

	return (
		<section className="panel update-notice" data-stage={stage.stage} aria-labelledby="updateTitle">
			<div className={'update-progress' + (stage.progress === null ? ' indeterminate' : '')} role="progressbar"
				aria-label="Update download" aria-valuemin={0} aria-valuemax={100}
				aria-valuenow={stage.progress === null ? undefined : Math.round(stage.progress * 100)}>
				<svg className="update-ring" viewBox="0 0 40 40" aria-hidden="true">
					<circle className="ring-track" cx="20" cy="20" r="17" />
					<circle className="ring-value" cx="20" cy="20" r="17" pathLength="100" style={{ strokeDashoffset: 100 - percent }} />
				</svg>
				<Icon name={glyph} className="update-glyph" />
			</div>
			<div className="update-body">
				<h2 id="updateTitle">{stage.title}</h2>
				<span>{stage.text}</span>
				{stage.detail && <span className="update-note">{stage.detail}</span>}
			</div>
			<div className="update-actions">
				<a className="docs-link" href={RELEASE_PAGE + offer.version} target="_blank" rel="noopener noreferrer">Release notes</a>
				{stage.stage === 'updated'
					? <button type="button" className="btn btn-secondary btn-sm" onClick={() => setHidden(true)}>Close</button>
					: busy ? <button type="button" className="btn btn-secondary btn-sm" onClick={cancel}>Cancel</button>
						: <button type="button" className="btn btn-quiet btn-sm" onClick={later}>Later</button>}
				{stage.stage === 'available' && offer.download && !offer.download.inApp && (
					<a className="btn btn-primary btn-sm" href={offer.download.url} download onClick={() => savePending(offer.version)}>Download</a>
				)}
				{stage.stage === 'available' && offer.download?.inApp && (
					<button type="button" className="btn btn-primary btn-sm" disabled={starting} onClick={() => void startDownload()}>Update</button>
				)}
				{stage.stage === 'failed' && <button type="button" className="btn btn-primary btn-sm" disabled={starting} onClick={() => void startDownload()}>Try again</button>}
				{stage.stage === 'ready' && <button type="button" className="btn btn-primary btn-sm" onClick={() => void postUpdate('show')}>Show in folder</button>}
			</div>
			<span className="visually-hidden" role="status">{stage.title}. {stage.text}</span>
		</section>
	);
}
