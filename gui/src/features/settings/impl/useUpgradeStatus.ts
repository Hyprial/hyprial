import { useCallback, useEffect, useRef, useState } from 'react';
import { ClientError } from '../../../client';
import type { AutoupdateStatusResult } from './views/contracts';
import type { UpgradeStatusApi } from './upgradeApi';

export type UpgradeStatusState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly value: AutoupdateStatusResult }
  | { readonly kind: 'failed' | 'unknown' | 'not-ready' };

export function useUpgradeStatus(api: UpgradeStatusApi) {
  const [snapshot, setSnapshot] = useState<{ api: UpgradeStatusApi; state: UpgradeStatusState }>({
    api, state: { kind: 'loading' },
  });
  const sequence = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    const generation = ++sequence.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSnapshot({ api, state: { kind: 'loading' } });
    try {
      const value = await api.status(controller.signal);
      if (!controller.signal.aborted && generation === sequence.current) {
        setSnapshot({ api, state: { kind: 'ready', value } });
      }
    } catch (error) {
      if (!controller.signal.aborted && generation === sequence.current) {
        const kind = error instanceof ClientError && ['HTTP_NOT_READY', 'DAEMON_NOT_READY', 'DAEMON_RESTORING'].includes(error.code)
          ? 'not-ready' : error instanceof ClientError && error.dispatch === 'unknown' ? 'unknown' : 'failed';
        setSnapshot({ api, state: { kind } });
      }
    }
  }, [api]);
  useEffect(() => {
    void refresh();
    return () => { ++sequence.current; pending.current?.abort(); };
  }, [api, refresh]);
  const state: UpgradeStatusState = snapshot.api === api ? snapshot.state : { kind: 'loading' };
  return { state, refresh };
}
