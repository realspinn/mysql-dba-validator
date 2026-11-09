import type { Status } from './status';

// A live line that says what an action did.
export function StatusLine({ value }: { value: Status }) {
	return <p className={'status-line' + (value.kind ? ' ' + value.kind : '')} role="status">{value.text}</p>;
}
