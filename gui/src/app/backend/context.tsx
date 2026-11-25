import { createContext, useContext, type ReactNode } from 'react';
import type { BackendState } from './types';

const unavailable: BackendState = Object.freeze({ status: 'unavailable' });
const BackendContext = createContext<BackendState>(unavailable);

/** No implicit connection, environment lookup or fixture fallback. */
export function BackendProvider({ value, children }: {
  readonly value: BackendState;
  readonly children: ReactNode;
}) {
  return <BackendContext.Provider value={value}>{children}</BackendContext.Provider>;
}

/** Common composition consumes the state; feature factories receive narrow clients. */
export function useBackend(): BackendState {
  return useContext(BackendContext);
}
