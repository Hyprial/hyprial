import { useEffect, useRef, useState } from 'react';
import type { AccountSnapshot, AccountProgress, FirstRunActions } from '../onboarding';
import { safeAuthorizationUrl } from '../onboarding/authorization-url';

const unknown: AccountSnapshot = {
  account: { status: 'unknown', message: '账号状态尚未确认。' },
  device: { status: 'unknown', message: '设备状态尚未确认。' },
};
/** Settings uses the same domain actions as onboarding, without wizard side effects. */
export function useAccountSettings(actions: FirstRunActions) {
  const [snapshot, setSnapshot] = useState(unknown);
  const [busy, setBusy] = useState<'read' | 'login' | 'device' | null>(null);
  const [progress, setProgress] = useState<AccountProgress | null>(null);
  const generation = useRef(0);
  const operation = useRef(false);
  const popup = useRef<Window | null>(null);
  const closeBlank = () => {
    const window = popup.current; popup.current = null;
    try { if (window && !window.closed && window.location.href === 'about:blank') window.close(); } catch { /* User-owned auth page. */ }
  };
  useEffect(() => {
    let disposed = false;
    const id = ++generation.current;
    setBusy('read');
    const unsubscribe = actions.subscribeAccountProgress?.(event => {
      if (disposed || !operation.current) return;
      setProgress(event);
      const url = safeAuthorizationUrl(event.verificationUri);
      const target = popup.current;
      if (url && target) {
        popup.current = null;
        try { if (!target.closed) target.location.replace(url); } catch { /* Visible link remains. */ }
      }
    });
    void (actions.getAccountState?.() ?? Promise.resolve(unknown)).then(value => {
      if (!disposed && id === generation.current) setSnapshot(value);
    }).catch(() => {
      if (!disposed && id === generation.current) setSnapshot({
        account: { status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。' },
        device: unknown.device,
      });
    }).finally(() => { if (!disposed && id === generation.current) setBusy(null); });
    return () => {
      disposed = true; generation.current += 1; unsubscribe?.(); closeBlank();
      if (operation.current) void actions.cancelAccount?.().catch(() => {});
      operation.current = false;
    };
  }, [actions]);

  async function run(kind: 'login' | 'device' | 'read') {
    if (operation.current) return;
    const id = ++generation.current;
    operation.current = true; setBusy(kind); setProgress(null);
    if (kind === 'login') {
      try { popup.current = window.open('about:blank', '_blank'); if (popup.current) popup.current.opener = null; } catch { /* Link fallback. */ }
    }
    try {
      if (kind === 'login') {
        const account = await actions.authenticateAccount({ account: '', token: '' }, 'login');
        if (id !== generation.current) return;
        setSnapshot(value => ({ ...value, account }));
      } else if (kind === 'device') {
        const device = await actions.initializeDevice();
        if (id !== generation.current) return;
        setSnapshot(value => ({ ...value, device }));
      }
      if (actions.getAccountState) {
        try {
          const value = await actions.getAccountState();
          if (id === generation.current) setSnapshot(value);
        } catch {
          if (id === generation.current && kind === 'read') {
            setSnapshot(value => ({ ...value, account: {
              status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。',
            } }));
          }
        }
      }
    } catch {
      if (id !== generation.current) return;
      setSnapshot((value): AccountSnapshot => kind === 'login'
        ? { ...value, account: value.account.status === 'authenticated'
            ? value.account
            : { status: 'unknown', message: '账号操作结果未确认，请刷新状态。' } }
        : kind === 'device'
          ? { ...value, device: value.device.status === 'ready'
              ? value.device
              : { status: 'unknown', message: '设备操作结果未确认，请刷新状态。' } }
          : { ...value, account: { status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。' } });
    } finally {
      if (id === generation.current) { operation.current = false; setBusy(null); closeBlank(); }
    }
  }
  async function cancel(): Promise<boolean> {
    const id = ++generation.current;
    const previous = snapshot;
    setProgress(null); closeBlank(); setBusy('read');
    operation.current = true;
    let confirmed = false;
    try {
      if (!actions.cancelAccount) throw new Error('CANCEL_UNAVAILABLE');
      const result = await actions.cancelAccount();
      let next: AccountSnapshot | undefined;
      try { next = await actions.getAccountState?.(); }
      catch { /* The cancellation remains unconfirmed. */ }
      if (id !== generation.current) return false;
      // A resolved promise is not cancellation confirmation: the native
      // acknowledgement and the refreshed authoritative state must both agree
      // before the operation gate is released for another login.
      if (result.ok === true && result.cancelled === true && next) {
        setSnapshot(next);
        confirmed = true;
      } else {
        setSnapshot(previous);
      }
    } catch { if (id === generation.current) setSnapshot(previous); }
    finally { if (id === generation.current) { operation.current = !confirmed; setBusy(null); } }
    return confirmed;
  }
  async function retry() {
    if (operation.current) {
      if (!await cancel()) return;
    }
    await run('login');
  }
  return { snapshot, busy, progress, run, cancel, retry };
}
