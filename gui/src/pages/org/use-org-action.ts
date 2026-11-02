import { useEffect, useRef, useState } from 'react';
import type { OrgActionOutcome } from './org-view-model';

export type OrgActionPhase = 'idle' | 'pending' | 'failed';

export interface OrgActionHandle {
  readonly phase: OrgActionPhase;
  readonly error: string | null;
  /** Start one local attempt. Ignored while unavailable or already pending. */
  submit: (failureMessage: string, onSuccess?: () => void) => void;
  readonly busy: boolean;
}

/** Local resolve delay so the pending state is observable and testable. */
export const ORG_ACTION_DELAY_MS = 300;

/**
 * Local submit state for one organization form.
 *
 * No interface exists in this build, so an attempt can only end in a local
 * failure. `pending` never resolves and exists so a test can prove duplicate
 * submits are blocked. Nothing here reports a success.
 */
export function useOrgAction(outcome: OrgActionOutcome): OrgActionHandle {
  const [phase, setPhase] = useState<OrgActionPhase>('idle');
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    },
    [],
  );

  const submit = (failureMessage: string, onSuccess?: () => void) => {
    if (outcome === 'unavailable' || phase === 'pending') {
      return;
    }
    if (outcome === 'success') {
      setError(null);
      onSuccess?.();
      return;
    }
    setError(null);
    setPhase('pending');
    if (outcome === 'pending') {
      return;
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setError(failureMessage);
      setPhase('failed');
    }, ORG_ACTION_DELAY_MS);
  };

  return { phase, error, submit, busy: phase === 'pending' };
}
