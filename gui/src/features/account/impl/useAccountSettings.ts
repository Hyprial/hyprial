/**
 * Decision record (settings account real chain, 2026-11-09)
 *
 * - A confirmed logout must move the displayed snapshot to the status receipt.
 *   When the action result carries no snapshot (the connected GeneratedClient
 *   port), the hook re-reads via getAccountState and adopts whatever the receipt
 *   projects — including unavailable/unknown; a rejected or missing read
 *   projects the shared unknown snapshot instead of keeping the stale owner.
 * - A failed logout also re-reads status. An inspect receipt keeps mutations
 *   disabled until the host is reopened; refresh never retries removal.
 * - run('read') is exempt from the operation gate: status reads dispatch no
 *   mutating request, so refresh stays usable while a cancellation is
 *   unconfirmed, and a successful read re-admits login by proving a terminal
 *   state. begin/logout keep the existing gates.
 * - A call refused before dispatch (catalog or composition) carries a
 *   `refusal` on its result. The user operation that met it is reported once
 *   through the notification port (`${subject}暂不可用。`, source `method · CODE`);
 *   the subject is the operated control's own label (a caller may pass it to
 *   `run`; 登录 names the operation of 登录 Hyprial and 重试登录)
 *   and leaves the snapshot, busy gate and logout state as they were; the
 *   section keeps its controls. The mount read is not an operation: a status
 *   refused before dispatch is adopted with its refusal, the status field and
 *   the initialisation block that depend on it render nothing (device cards with
 *   ports still render), and nothing is
 *   notified. A read that was dispatched and failed keeps its failed state.
 *   The raw session returns the refusal and the consumer hook reports it, so
 *   the shared session mounted above the notification provider never needs it;
 *   AppShell passes its own notifier.
 * - Refusals known before any side effect (loginRefusal, cancelRefusal on the
 *   action port, synchronous) are checked first: a refused login opens no
 *   window and sets no busy state, a refused cancellation neither closes the
 *   running login's window nor supersedes it. retry() checks the login
 *   refusal before it cancels or reads anything.
 * - An unreconciled login receipt (snapshot.pendingLogin, the 核对 state) takes
 *   precedence: the refusal pre-check is skipped so the port's own "verify the
 *   previous attempt" answer is the one the user gets.
 * - An unknown-state retry reads status first. With no read entry point the
 *   retry falls through to the login action itself (unavailable sources then
 *   surface their refusal); a failed read projects the explicit unavailable
 *   status instead of returning silently.
 */
import { createContext, createElement, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { MethodRefusal } from '../../../client';
import { unavailableNotice, useNotify, type Notify } from '../../../components/notifications';
import type { AccountSnapshot, AccountProgress, AccountLogoutResult, FirstRunActions } from '../../onboarding';
import { safeAuthorizationUrl } from '../../onboarding';

const unknown: AccountSnapshot = {
  account: { status: 'unknown', message: '暂时无法读取账号信息，请刷新重试。' },
  device: { status: 'unknown', message: '设备状态尚未确认。' },
};
/** Settings uses the same domain actions as onboarding, without wizard side effects. */
interface AccountSession {
  snapshot: AccountSnapshot;
  busy: 'read' | 'login' | 'device' | 'logout' | null;
  statusLoaded: boolean;
  logoutState: AccountLogoutResult | null;
  progress: AccountProgress | null;
  run: (kind: 'login' | 'device' | 'read', label?: string) => Promise<void>;
  logout: () => Promise<void>;
  cancel: () => Promise<boolean>;
  retry: () => Promise<void>;
  canLogout: boolean;
  hasLogout: boolean;
  hasLogin: boolean;
  hasDeviceInit: boolean;
}
interface Refusal { readonly subject: string; readonly detail: MethodRefusal }
interface RawSession extends Omit<AccountSession, 'run' | 'logout' | 'cancel' | 'retry'> {
  run: (kind: 'login' | 'device' | 'read') => Promise<Refusal | null>;
  logout: () => Promise<Refusal | null>;
  cancel: () => Promise<{ confirmed: boolean; refusal: Refusal | null }>;
  retry: () => Promise<Refusal | null>;
}
const AccountSessionContext = createContext<{ actions: FirstRunActions; session: RawSession } | null>(null);

function statusRefusal(snapshot: AccountSnapshot): MethodRefusal | null {
  return snapshot.account.status === 'unavailable' ? snapshot.account.refusal ?? null : null;
}
function resultRefusal(result: { readonly status: string; readonly refusal?: MethodRefusal }): MethodRefusal | null {
  return result.status === 'unavailable' ? result.refusal ?? null : null;
}

export function AccountSessionProvider({ actions, children }: { readonly actions: FirstRunActions; readonly children: ReactNode }) {
  const session = useLocalAccountSettings(actions, null);
  return createElement(AccountSessionContext.Provider, { value: { actions, session } }, children);
}

export function useAccountSettings(actions: FirstRunActions, notifier?: Notify): AccountSession {
  const shared = useContext(AccountSessionContext);
  const raw = useLocalAccountSettings(actions, shared?.actions === actions ? shared.session : null);
  const providerNotify = useNotify();
  const notify = notifier ?? providerNotify;
  const report = (refusal: Refusal | null) => {
    if (refusal) notify(unavailableNotice(refusal.subject, refusal.detail));
  };
  return {
    snapshot: raw.snapshot, busy: raw.busy, statusLoaded: raw.statusLoaded, logoutState: raw.logoutState, progress: raw.progress,
    canLogout: raw.canLogout, hasLogout: raw.hasLogout, hasLogin: raw.hasLogin, hasDeviceInit: raw.hasDeviceInit,
    run: async (kind, label) => {
      const refusal = await raw.run(kind);
      report(refusal && label ? { ...refusal, subject: label } : refusal);
    },
    logout: async () => report(await raw.logout()),
    cancel: async () => {
      const { confirmed, refusal } = await raw.cancel();
      report(refusal);
      return confirmed;
    },
    retry: async () => report(await raw.retry()),
  };
}

function useLocalAccountSettings(actions: FirstRunActions, shared: RawSession | null): RawSession {
  const [snapshot, setSnapshot] = useState(unknown);
  const source = useRef(actions);
  const [busy, setBusy] = useState<'read' | 'login' | 'device' | 'logout' | null>(null);
  const [statusLoaded, setStatusLoaded] = useState(false);
  const [logoutState, setLogoutState] = useState<AccountLogoutResult | null>(null);
  const [progress, setProgress] = useState<AccountProgress | null>(null);
  const generation = useRef(0);
  const operation = useRef(false);
  const cancelledSnapshot = useRef<AccountSnapshot | null>(null);
  const popup = useRef<Window | null>(null);
  const closeBlank = () => {
    const window = popup.current; popup.current = null;
    try { if (window && !window.closed && window.location.href === 'about:blank') window.close(); } catch { /* User-owned auth page. */ }
  };
  useEffect(() => {
    if (shared !== null) return;
    let disposed = false;
    const id = ++generation.current;
    source.current = actions;
    setSnapshot(unknown);
    setStatusLoaded(false);
    setBusy('read'); setLogoutState(null);
    const unsubscribe = actions.subscribeAccountProgress?.(event => {
      if (disposed || !operation.current) return;
      setProgress(event);
      const url = safeAuthorizationUrl(event.verificationUri);
      const target = popup.current;
      if (url && target && !actions.openAuthorization) {
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
    }).finally(() => {
      if (!disposed && id === generation.current) { setBusy(null); setStatusLoaded(true); }
    });
    return () => {
      // Unmount stops polling and drops stale responses. It never cancels the
      // server-side operation implicitly: that is an explicit user action.
      disposed = true; generation.current += 1; unsubscribe?.(); closeBlank();
      operation.current = false;
    };
  }, [actions, shared !== null]);

  useEffect(() => {
    if (shared !== null || !snapshot.pendingLogin || !actions.readLoginOperation) return;
    let disposed = false;
    const id = generation.current;
    const pending = snapshot.pendingLogin;
    void actions.readLoginOperation(pending.operationId).then(account => {
      if (disposed || id !== generation.current) return;
      setSnapshot(previous => previous.pendingLogin !== pending ? previous : {
        account, device: previous.device,
        ...(['unknown', 'unavailable'].includes(account.status) ? { pendingLogin: pending } : {}),
      });
    }).catch(() => undefined);
    return () => { disposed = true; };
  }, [actions, shared !== null, snapshot.pendingLogin]);

  async function run(kind: 'login' | 'device' | 'read'): Promise<Refusal | null> {
    if (kind === 'login' && !actions.authenticateAccount) return null;
    if (kind === 'device' && !actions.initializeDevice) return null;
    if (kind !== 'read' && (operation.current || logoutState?.status === 'unknown' || logoutState?.nextAction === 'inspect')) return null;
    if (kind === 'login' && !snapshot.pendingLogin) {
      const detail = actions.loginRefusal?.();
      if (detail) return { subject: '登录', detail };
    }
    let refusal: Refusal | null = null;
    const id = ++generation.current;
    operation.current = true; setBusy(kind); setProgress(null);
    if (kind === 'login' && !actions.openAuthorization) {
      try { popup.current = window.open('about:blank', '_blank'); if (popup.current) popup.current.opener = null; } catch { /* Link fallback. */ }
    }
    try {
      if (kind === 'login') {
        const account = await actions.authenticateAccount!({ account: '', token: '' }, 'login');
        if (id !== generation.current) return null;
        const detail = resultRefusal(account);
        if (detail) return { subject: '登录', detail };
        setSnapshot(value => ({ ...value, account }));
      } else if (kind === 'device') {
        const device = await actions.initializeDevice!();
        if (id !== generation.current) return null;
        const detail = resultRefusal(device);
        if (detail) return { subject: '初始化当前设备', detail };
        setSnapshot(value => ({ ...value, device }));
      }
      if (actions.getAccountState) {
        try {
          const value = await actions.getAccountState();
          const detail = statusRefusal(value);
          if (detail) {
            if (kind === 'read' && id === generation.current) refusal = { subject: '刷新账号状态', detail };
          } else if (id === generation.current) {
            setSnapshot(previous => kind === 'device' && value.account.status === 'authenticated'
              && value.device.status !== 'ready' && ['failed', 'unavailable', 'unknown'].includes(previous.device.status)
              ? { ...value, device: previous.device } : value);
            if (kind === 'read') setLogoutState(previous => previous?.nextAction === 'inspect' ? previous : null);
          }
        } catch {
          if (id === generation.current && kind === 'read') {
            setSnapshot(value => ({ ...value, account: {
              status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。',
            } }));
          }
        }
      }
    } catch {
      if (id !== generation.current) return null;
      setSnapshot((value): AccountSnapshot => kind === 'login'
        ? { ...value, account: value.account.status === 'authenticated'
            ? value.account
            : { status: 'unknown', message: '未收到登录结果，请刷新账号状态。' } }
        : kind === 'device'
          ? { ...value, device: value.device.status === 'ready'
              ? value.device
              : { status: 'unknown', message: '设备操作结果未确认，请刷新状态。' } }
          : { ...value, account: { status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。' } });
    } finally {
      if (id === generation.current) { operation.current = false; setBusy(null); closeBlank(); }
    }
    return refusal;
  }
  async function logout(): Promise<Refusal | null> {
    if (operation.current || busy !== null || !actions.logoutAccount || logoutState?.status === 'unknown' || logoutState?.nextAction === 'inspect') return null;
    const id = ++generation.current;
    operation.current = true; setBusy('logout'); setProgress(null); setLogoutState(null);
    try {
      const result = await actions.logoutAccount();
      if (id !== generation.current) return null;
      if (result.refusal) return { subject: '退出本机登录', detail: result.refusal };
      setLogoutState(result);
      if (result.status === 'logged-out' || result.status === 'failed') {
        setSnapshot(unknown);
        if (result.snapshot) {
          setSnapshot(result.snapshot);
        } else {
          let next: AccountSnapshot | null = null;
          try { next = (await actions.getAccountState?.()) ?? null; } catch { next = null; }
          if (id === generation.current) setSnapshot(next ?? unknown);
        }
      } else if (result.status === 'unknown') setSnapshot(unknown);
    } catch {
      if (id === generation.current) {
        setSnapshot(unknown);
        setLogoutState({ status: 'unknown', message: '退出结果未知，请刷新账号状态确认；不会自动重复退出。' });
      }
    } finally {
      if (id === generation.current) { operation.current = false; setBusy(null); }
    }
    return null;
  }
  async function cancel(): Promise<{ confirmed: boolean; refusal: Refusal | null }> {
    const upfront = actions.cancelRefusal?.();
    if (upfront) return { confirmed: false, refusal: { subject: '取消授权', detail: upfront } };
    const id = ++generation.current;
    const previous = snapshot;
    cancelledSnapshot.current = null;
    setProgress(null); closeBlank(); setBusy('read');
    operation.current = true;
    let confirmed = false;
    let refusal: Refusal | null = null;
    try {
      if (!actions.cancelAccount) throw new Error('CANCEL_UNAVAILABLE');
      const result = await actions.cancelAccount();
      if (result.refusal) refusal = { subject: '取消授权', detail: result.refusal };
      let next: AccountSnapshot | undefined;
      try { next = await actions.getAccountState?.(); }
      catch { /* The cancellation remains unconfirmed. */ }
      if (id !== generation.current) return { confirmed: false, refusal: null };
      // A resolved promise is not cancellation confirmation: the native
      // acknowledgement and the refreshed authoritative state must both agree
      // before the operation gate is released for another login.
      if (result.ok === true && result.cancelled === true && next) {
        cancelledSnapshot.current = next;
        setSnapshot(next);
        confirmed = true;
      } else {
        setSnapshot(previous);
      }
    } catch { if (id === generation.current) setSnapshot(previous); }
    finally { if (id === generation.current) { operation.current = !confirmed; setBusy(null); } }
    return { confirmed, refusal };
  }
  async function retry(): Promise<Refusal | null> {
    if (logoutState?.nextAction === 'inspect') return null;
    if (snapshot.account.status !== 'authenticated' && !snapshot.pendingLogin) {
      const detail = actions.loginRefusal?.();
      if (detail) return { subject: '登录', detail };
    }
    if (operation.current) {
      const cancelled = await cancel();
      if (!cancelled.confirmed) return cancelled.refusal;
      const recovered = cancelledSnapshot.current;
      cancelledSnapshot.current = null;
      if (recovered?.account.status === 'authenticated') return null;
    }
    // Unknown/pending work is never re-sent: read status/receipt first and only
    // proceed when the server proves a terminal (unauthenticated) state.
    if (snapshot.account.status === 'unknown') {
      if (!actions.getAccountState) return run('login');
      let current: AccountSnapshot | undefined;
      try { current = await actions.getAccountState(); } catch { current = undefined; }
      if (!current) {
        setSnapshot((value): AccountSnapshot => value.account.status === 'unknown'
          ? { ...value, account: { status: 'unavailable', message: '账号服务不可用，请检查本机服务后刷新状态。' } }
          : value);
        return null;
      }
      const detail = statusRefusal(current);
      if (detail) return { subject: '登录', detail };
      setSnapshot(current);
      if (current.account.status !== 'unauthenticated') return null;
    }
    if (snapshot.account.status === 'authenticated') return null;
    return run('login');
  }
  return shared ?? { snapshot: source.current === actions ? snapshot : unknown,
    statusLoaded: source.current === actions && statusLoaded, busy, progress, run, cancel, retry, logout, logoutState,
    hasLogout: typeof actions.logoutAccount === 'function',
    hasLogin: typeof actions.authenticateAccount === 'function',
    hasDeviceInit: typeof actions.initializeDevice === 'function',
    canLogout: logoutState?.status !== 'unknown' && logoutState?.nextAction !== 'inspect' };
}
