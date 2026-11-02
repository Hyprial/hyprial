import { useEffect, useMemo, useState } from 'react';
import { ChatController, HttpLiveClient, LarkController, type LivePort } from '../../adapters/live';
export interface LiveRuntime {
  readonly status: 'loading' | 'ready' | 'error';
  readonly error?: string;
  readonly chat: ChatController;
  readonly lark: LarkController;
}
export function useLiveRuntime(enabled: boolean, injected?: LivePort): LiveRuntime | null {
  const services = useMemo(() => {
    if (!enabled) return null;
    const client = injected ?? new HttpLiveClient();
    return { client, chat: new ChatController(client), lark: new LarkController(client) };
  }, [enabled, injected]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'disabled'>('loading');
  const [error, setError] = useState<string>();
  useEffect(() => {
    if (!services) return;
    let alive = true;
    let timer: ReturnType<typeof setInterval> | undefined;
    setStatus('loading'); setError(undefined);
    void (async () => {
      try {
        if (services.client instanceof HttpLiveClient && !(await services.client.bootstrap())) {
          if (alive) setStatus('disabled');
          return;
        }
        if (!alive) return;
        setStatus('ready');
        await Promise.all([services.chat.start(), services.lark.refresh()]);
        if (!alive) return;
        timer = setInterval(() => {
          if (document.visibilityState === 'hidden') return;
          void services.chat.poll(); void services.lark.poll();
        }, 3_000);
      } catch {
        if (alive) { setStatus('error'); setError('真实服务尚未连接。请检查服务配置后刷新页面。'); }
      }
    })();
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [services]);
  return services && status !== 'disabled' ? { status, error, chat: services.chat, lark: services.lark } : null;
}
