// Tells the desktop app this page is still open, so it does not stop for being idle.
// Carries no data; a failure only means the app is not running. Not in the online demo.
import { useEffect } from 'react';
import { sendHeartbeat } from '../api/local';

const HEARTBEAT_MS = 60000;

export function useHeartbeat(enabled: boolean): void {
	useEffect(() => {
		if (!enabled) return;
		sendHeartbeat();
		const timer = window.setInterval(sendHeartbeat, HEARTBEAT_MS);
		const visible = () => { if (document.visibilityState === 'visible') sendHeartbeat(); };
		document.addEventListener('visibilitychange', visible);
		return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
	}, [enabled]);
}
