// A one-line result of an action: neutral, success or error.
export interface Status { text: string; kind: '' | 'success' | 'error' }
export const status = (text: string, kind: Status['kind'] = ''): Status => ({ text, kind });
