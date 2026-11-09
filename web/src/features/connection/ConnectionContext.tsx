import { createContext, useContext, type ReactNode } from 'react';
import { useConnection } from './useConnection';

type Connection = ReturnType<typeof useConnection>;
const Context = createContext<Connection | null>(null);

// One connection for the whole workspace: SQL review, observed performance and lock
// diagnostics all use the same target, login and database.
export function ConnectionProvider({ children }: { children: ReactNode }) {
	return <Context.Provider value={useConnection()}>{children}</Context.Provider>;
}

export function useConnectionContext(): Connection {
	const value = useContext(Context);
	if (!value) throw new Error('useConnectionContext outside ConnectionProvider');
	return value;
}
