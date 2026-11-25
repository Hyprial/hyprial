/**
 * Decision record (A7 account controls, 2026-11-07)
 *
 * - Every account action goes through the one GeneratedClient
 *   (auth.login.begin -> auth.operation.get -> terminal, cancel, relogin,
 *   logout, onboarding.status). No fetch, no path joining, no second transport.
 * - The receipt is the only truth: a terminal answer comes from the server
 *   receipt, never from the begin response or a local guess. A late poll after a
 *   cancel loses to the final receipt; the last write wins by receipt state.
 * - One operation may be in flight. A StrictMode/double mount reuses the same
 *   in-flight promise instead of beginning again; a remount only re-reads
 *   status/receipt. Unmount stops polling and never cancels the server
 *   operation implicitly.
 * - Polling has one absolute deadline computed at begin time (never accumulated)
 *   and every response is discarded when its generation is stale.
 * - unknown is never retried automatically. A manual retry reads status/receipt
 *   first; a new operation key is only minted after a proven terminal failure.
 * - Status is one observation. Pending login receipts belong to the login
 *   controller; reading status never advances authentication or replays writes.
 * - Concurrent receipt readers share one in-flight read per operation. A
 *   terminal result remains available to delayed consumers of that observation;
 *   status refresh and admitted account writes invalidate it. Unknown results
 *   are not cached, and changed selectors still fence late responses.
 * - Session storage never holds a token. Only the public operation key lives in
 *   memory; every value shown here is a public receipt field.
 */
import {
  ClientError, DomainError, TransportError, accessRefusal, type MethodAccess, type MethodRefusal,
} from '../../../client';
import type { GeneratedClient } from '../../../client/generated';
import type { OnboardingStatusResult } from '../../../client/generated/types';
import type { AuthCancelResult } from '../../../platform/auth';
import { firstRunAttemptView } from '../../onboarding';
import type {
  AccountActionResult,
  AccountLogoutResult,
  AccountProgress,
  AccountSnapshot,
  DeviceActionResult,
  FirstRunActions,
  FirstRunAttemptView,
  FirstRunReceiptView,
} from '../../onboarding';

type AccountClient = Pick<GeneratedClient, 'auth' | 'onboarding'>;

const LOGIN_BUDGET_MS = 180_000;
const POLL_INTERVAL_MS = 2_000;
const MAX_POLL_INTERVAL_MS = 5_000;
const TERMINAL_LOGIN = new Set(['authenticated', 'cancelled', 'denied', 'expired', 'failed']);

export interface ConnectedAccountPort {
  readonly actions: Pick<
    FirstRunActions,
    | 'authenticateAccount'
    | 'cancelAccount'
    | 'cancelRefusal'
    | 'loginRefusal'
    | 'getAccountState'
    | 'readLoginOperation'
    | 'logoutAccount'
    | 'subscribeAccountProgress'
  >;
  /** True while one admitted operation is still polled by this port. */
  readonly busy: () => boolean;
  /** The last operation id this port began (public selector only). */
  readonly operationId: () => string | null;
}

interface ActiveOperation {
  readonly key: string;
  readonly operationId: string | null;
  readonly deadlineAt: number;
  readonly promise: Promise<AccountActionResult>;
}

function unavailable(message: string): AccountActionResult {
  return { status: 'unavailable', message };
}
function failed(message: string, retryable = true): AccountActionResult {
  return { status: 'failed', message, retryable };
}
function unknown(message: string): AccountActionResult {
  return { status: 'unknown', message };
}

function refused(access: MethodAccess | undefined, method: string): MethodRefusal | null {
  return accessRefusal(access, method);
}

/** The bounded artifact labels a domain error reported, when it carried any. */
function removedLabels(error: ClientError): readonly string[] | undefined {
  if (!(error instanceof DomainError)) return undefined;
  const data = error.data;
  if (data === null || typeof data !== 'object') return undefined;
  const removed = (data as { readonly removed?: unknown }).removed;
  if (!Array.isArray(removed) || removed.length === 0) return undefined;
  if (!removed.every((label) => typeof label === 'string' && label.length > 0)) {
    return undefined;
  }
  return removed as readonly string[];
}

function firstRun(status: OnboardingStatusResult): FirstRunReceiptView {
  const receipt = status.firstRun;
  if (!receipt) {
    return { state: 'unavailable', revision: 0, confirmed: [], updatedAt: null, skipped: false };
  }
  return {
    state: receipt.state,
    revision: receipt.revision,
    confirmed: [...receipt.confirmed],
    updatedAt: receipt.updatedAt,
    skipped: receipt.skipped,
  };
}

function firstRunAttempt(status: OnboardingStatusResult): FirstRunAttemptView | undefined {
  return status.firstRunAttempt ? firstRunAttemptView(status.firstRunAttempt) ?? undefined : undefined;
}

function device(status: OnboardingStatusResult): DeviceActionResult {
  if (status.authenticated && status.deviceReady && status.initialized && status.phase === 'serving') return { status: 'ready' };
  if (status.operations.some(item => ['device', 'initialize'].includes(item.kind) && ['pending', 'accepted', 'promoting', 'unknown'].includes(item.state))) {
    return { status: 'unknown', message: '初始化结果尚未确认，请刷新状态。' };
  }
  if (status.authenticated && status.deviceReady) {
    return { status: 'not-ready', message: '设备已登录，但初始化状态尚未完成。' };
  }
  return { status: 'not-ready' };
}

/**
 * Project one status receipt. Pending work is reported as unknown, never as a
 * success: only the server receipt can say an operation finished.
 */
export function projectAccountStatus(status: OnboardingStatusResult): AccountSnapshot {
  const attempt = firstRunAttempt(status);
  if (status.authenticated) {
    const username = (status as { readonly username?: string | null }).username;
    return {
      account: {
        status: 'authenticated',
        ...(status.owner ? { owner: status.owner } : {}),
        ...(username ? { username } : {}),
      },
      device: device(status),
      firstRun: firstRun(status),
      ...(attempt ? { firstRunAttempt: attempt } : {}),
    };
  }
  // A non-owner home may still hold an unresolved login receipt.  Neither a
  // pending nor an ``unknown`` receipt is a signed-out home: both keep the
  // operation id so the caller can reconcile instead of starting a second login.
  const unresolved = status.operations.find(
    (operation) =>
      operation.kind === 'login'
      && (operation.state === 'pending' || operation.state === 'unknown'),
  );
  if (unresolved) {
    return {
      pendingLogin: { operationId: unresolved.operationId },
      account: {
        status: 'unknown',
        message:
          unresolved.state === 'pending'
            ? '正在等待授权完成，请在授权页继续。'
            : '未收到登录结果，请刷新账号状态。',
      },
      device: device(status),
      firstRun: firstRun(status),
    };
  }
  const ended = [...status.operations].reverse().find(operation => operation.kind === 'login');
  if (ended?.state === 'failed') {
    return {
      account: failed('授权失败或账号冲突，请检查提示后重试。', false),
      device: device(status),
      firstRun: firstRun(status),
    };
  }
  if (ended?.state === 'expired' || ended?.state === 'denied') {
    return {
      account: failed(ended.state === 'expired' ? '授权已过期，请重新登录。' : '授权未获允许，请重新登录。'),
      device: device(status),
      firstRun: firstRun(status),
    };
  }
  return {
    account: { status: 'unauthenticated' },
    device: device(status),
    firstRun: firstRun(status),
  };
}

function newOperationKey(): string {
  const crypto_ = globalThis.crypto;
  if (crypto_ && typeof crypto_.randomUUID === 'function') return crypto_.randomUUID();
  return `opkey-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('aborted', 'AbortError'));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    function onAbort(): void {
      clearTimeout(timer);
      reject(new DOMException('aborted', 'AbortError'));
    }
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export function createConnectedAccountPort(
  client: AccountClient,
  access?: MethodAccess,
): ConnectedAccountPort {
  const inFlight: { current: ActiveOperation | null } = { current: null };
  const receiptReads = new Map<string, Promise<AccountActionResult>>();
  let reconciledLogin: { operationId: string; account: AccountActionResult } | null = null;
  let lastOperationId: string | null = null;
  // The last receipt projection, used only to choose login vs same-owner
  // relogin. It is never a credential and never survives a reload.
  let lastStatus: AccountSnapshot | null = null;
  // The one login receipt this port has not reconciled yet.  It is public data
  // (an operation id) and it is what stops a retry from opening a second login.
  let unreconciledLoginId: string | null = null;
  const listeners = new Set<(progress: AccountProgress) => void>();

  const notify = (progress: AccountProgress): void => {
    for (const listener of [...listeners]) {
      try {
        listener(progress);
      } catch {
        // A listener failure never changes the operation outcome.
      }
    }
  };

  async function readStatus(): Promise<AccountSnapshot> {
    reconciledLogin = null;
    const refusal = refused(access, 'onboarding.status');
    if (refusal) {
      const unavailableSnapshot: AccountSnapshot = {
        account: { status: 'unavailable', refusal },
        device: { status: 'unavailable', refusal },
        firstRun: { state: 'unavailable', revision: 0, confirmed: [], updatedAt: null, skipped: false },
      };
      return unavailableSnapshot;
    }
    try {
      const receipt = await client.onboarding.status({});
      reconciledLogin = null;
      unreconciledLoginId =
        receipt.operations.find(
          (operation) =>
            operation.kind === 'login'
            && (operation.state === 'pending' || operation.state === 'unknown'),
        )?.operationId ?? null;
      const snapshot = projectAccountStatus(receipt);
      lastStatus = snapshot;
      return snapshot;
    } catch (error) {
      reconciledLogin = null;
      return {
        account: unavailable(error instanceof TransportError && error.kind === 'network'
          ? '无法连接本机服务，请重新打开 Hyprial。'
          : error instanceof ClientError && ['SESSION_REQUIRED', 'SESSION_EXPIRED', 'UNAUTHORIZED'].includes(error.code)
            ? '界面会话已失效，请重新打开 Hyprial。'
            : '暂时无法读取账号信息，请刷新重试。'),
        device: { status: 'unknown', message: '设备状态尚未确认。' },
        firstRun: { state: 'unavailable', revision: 0, confirmed: [], updatedAt: null, skipped: false },
      };
    }
  }

  function readLoginOperation(operationId: string): Promise<AccountActionResult> {
    if (inFlight.current) return inFlight.current.promise;
    const refusal = refused(access, 'auth.operation.get');
    if (refusal) return Promise.resolve({ status: 'unavailable', refusal });
    if (reconciledLogin?.operationId === operationId) return Promise.resolve(reconciledLogin.account);
    const existing = receiptReads.get(operationId);
    if (existing) return existing;
    const pending = readLoginReceipt(operationId).finally(() => {
      if (receiptReads.get(operationId) === pending) receiptReads.delete(operationId);
    });
    receiptReads.set(operationId, pending);
    return pending;
  }

  async function readLoginReceipt(operationId: string): Promise<AccountActionResult> {
    if (operationId !== unreconciledLoginId) return unknown('登录操作已变化，请刷新账号状态。');
    try {
      const { operation } = await client.auth.operation.get({ operationId });
      if (operationId !== unreconciledLoginId) return unknown('登录操作已变化，请刷新账号状态。');
      if (operation.kind !== 'login' || operation.operationId !== operationId) return unknown('授权回执不匹配，请刷新账号状态。');
      let account: AccountActionResult;
      if (operation.state === 'authenticated') {
        account = { status: 'authenticated', ...(operation.owner ? { owner: operation.owner } : {}) };
      } else if (operation.state === 'failed') {
        const opError = (operation as {
          readonly error?: { readonly code?: string; readonly message?: string } | null;
        }).error;
        account = failed(
          opError?.message ?? '授权失败，请重新登录。',
          opError?.code !== 'IDENTITY_CONFLICT',
        );
      } else if (operation.state === 'expired' || operation.state === 'denied') {
        account = failed(operation.state === 'expired' ? '授权已过期，请重新登录。' : '授权未获允许，请重新登录。');
      } else if (operation.state === 'cancelled') {
        account = { status: 'unauthenticated' };
      } else {
        return unknown(operation.state === 'pending' ? '正在等待授权完成，请在授权页继续。' : '未收到登录结果，请刷新账号状态。');
      }
      unreconciledLoginId = null;
      reconciledLogin = { operationId, account };
      if (lastStatus) lastStatus = { account, device: lastStatus.device };
      return account;
    } catch {
      return unknown('无法读取授权回执，请刷新账号状态。');
    }
  }

  async function pollLogin(
    operationId: string,
    budgetDeadlineAt: number,
    signal?: AbortSignal,
  ): Promise<AccountActionResult> {
    // The public verification window is the absolute upper bound: polling never
    // outlives the code the user was shown, and the interval is the one the
    // server published (never a local constant, never faster after a slow-down).
    let deadlineAt = budgetDeadlineAt;
    let interval = POLL_INTERVAL_MS;
    while (true) {
      const remaining = deadlineAt - Date.now();
      if (remaining <= 0) {
        return unknown(`授权操作 ${operationId} 未在期限内确认；不会自动重发。`);
      }
      let receipt;
      try {
        receipt = await client.auth.operation.get(
          { operationId },
          { signal, budgetMs: Math.max(1_000, remaining) },
        );
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error;
        const lost = error instanceof ClientError && error.dispatch === 'unknown';
        return lost
          ? unknown(`授权操作 ${operationId} 的最新回执未知；不会自动重发。`)
          : failed('无法读取授权回执，请手动重试。');
      }
      const operation = receipt.operation;
      if (operation.kind === 'logout') {
        return failed('服务端返回了不匹配的回执类型。', false);
      }
      if (operation.verification) {
        const expiresAt = Date.parse(operation.verification.expiresAt);
        if (Number.isFinite(expiresAt)) {
          // Absolute bound from the receipt, not an accumulated interval.
          deadlineAt = Math.min(deadlineAt, expiresAt);
        }
        const published = operation.verification.intervalSeconds;
        if (Number.isFinite(published) && published > 0) {
          // The server owns the schedule: a slow-down is honoured and a shorter
          // local interval can never make the worker poll the issuer earlier.
          interval = published * 1_000;
        }
        notify({
          event: 'device',
          verificationUri: operation.verification.verificationUri,
          userCode: operation.verification.userCode,
          expiresIn: Math.max(0, expiresAt - Date.now()),
          interval: operation.verification.intervalSeconds,
        });
      }
      if (operation.state === 'authenticated') {
        const stage = operation.completion?.stage;
        if (!operation.completion || stage === 'serving') {
          notify({ event: 'authenticated', ...(operation.owner ? { owner: operation.owner } : {}) });
          return { status: 'authenticated', ...(operation.owner ? { owner: operation.owner } : {}) };
        }
        if (stage === 'stopped') {
          return failed(operation.completion.failure?.message ?? '授权后服务未就绪，请检查状态后重试。', true);
        }
      } else if (TERMINAL_LOGIN.has(operation.state)) {
        if (operation.state === 'cancelled') {
          return failed('授权已取消。', false);
        }
        if (operation.state === 'failed') {
          const opError = (operation as {
            readonly error?: { readonly code?: string; readonly message?: string } | null;
          }).error;
          return failed(
            opError?.message ?? '授权失败，请重新登录。',
            opError?.code !== 'IDENTITY_CONFLICT',
          );
        }
        return failed(`授权未完成（${operation.state}）。`, true);
      } else if (operation.state !== 'pending') {
        if (operation.completion?.stage === 'stopped' && operation.completion.failure) {
          return failed(operation.completion.failure.message, true);
        }
        return unknown(`授权操作 ${operation.operationId} 的回执状态未知；不会自动重发。`);
      }
      interval = Math.min(MAX_POLL_INTERVAL_MS, Math.max(POLL_INTERVAL_MS, interval));
      const wait = Math.min(interval, Math.max(0, deadlineAt - Date.now()));
      if (wait <= 0) {
        return unknown(`授权操作 ${operationId} 未在期限内确认；不会自动重发。`);
      }
      await sleep(wait, signal);
    }
  }

  function loginRefusal(): MethodRefusal | null {
    const method = lastStatus?.account.status === 'authenticated' ? 'auth.relogin' : 'auth.login.begin';
    return refused(access, method) ?? refused(access, 'auth.operation.get');
  }

  function cancelRefusal(): MethodRefusal | null {
    return refused(access, 'auth.operation.cancel');
  }

  async function beginLogin(operationKey: string, signal?: AbortSignal): Promise<AccountActionResult> {
    // An already-authenticated home uses the same-owner relogin alias; a fresh
    // home begins an ordinary login. Both are GeneratedClient calls.
    const relogin = lastStatus?.account.status === 'authenticated';
    const refusal = loginRefusal();
    if (refusal) return { status: 'unavailable', refusal };
    const deadlineAt = Date.now() + LOGIN_BUDGET_MS;
    let operationId: string | null = null;
    try {
      const begin = relogin
        ? await client.auth.relogin({ operationKey, until: 'serving' }, { signal, budgetMs: LOGIN_BUDGET_MS })
        : await client.auth.login.begin(
            { intent: 'login', operationKey, until: 'serving' },
            { signal, budgetMs: LOGIN_BUDGET_MS },
          );
      operationId = begin.operationId;
      lastOperationId = operationId;
      if (begin.verification) {
        notify({
          event: 'device',
          verificationUri: begin.verification.verificationUri,
          userCode: begin.verification.userCode,
          expiresIn: Math.max(0, Date.parse(begin.verification.expiresAt) - Date.now()),
          interval: begin.verification.intervalSeconds,
        });
      }
      // Even a terminal begin projection must be confirmed by its receipt.
      return await pollLogin(begin.operationId, deadlineAt, signal);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      if (error instanceof ClientError) {
        if (error.dispatch === 'unknown' || error.code === 'SUBMIT_OUTCOME_UNKNOWN') {
          return unknown(
            operationId
              ? `授权操作 ${operationId} 的提交结果未知；不会自动重发。`
              : '授权提交结果未知；不会自动重发。',
          );
        }
        return failed(error.message || '无法开始授权。', true);
      }
      return failed('无法开始授权。', true);
    }
  }

  function authenticateAccount(
    _values: { readonly account: string; readonly token: string },
    mode: 'login' | 'register',
    options: { readonly signal?: AbortSignal } = {},
  ): Promise<AccountActionResult> {
    if (mode === 'register') {
      return Promise.resolve(
        unavailable('本机不提供注册；请使用官方授权页登录。'),
      );
    }
    // Double mount / route switch: reuse the one in-flight operation instead of
    // beginning a second one. The hook's own guard already refuses a second
    // user-initiated begin while busy.
    if (inFlight.current) return inFlight.current.promise;
    // An unresolved receipt must be read, not replaced: a new key here would
    // start a second login behind the user's back.
    if (unreconciledLoginId !== null) {
      return Promise.resolve(
        unknown(
          `授权操作 ${unreconciledLoginId} 的结果仍需核对；请先刷新账号状态，不会自动重发。`,
        ),
      );
    }
    const key = newOperationKey();
    reconciledLogin = null;
    const promise = (async () => {
      try {
        return await beginLogin(key, options.signal);
      } finally {
        if (inFlight.current?.key === key) inFlight.current = null;
      }
    })();
    inFlight.current = { key, operationId: lastOperationId, deadlineAt: Date.now() + LOGIN_BUDGET_MS, promise };
    return promise;
  }

  async function receiptCancelled(operationId: string): Promise<boolean | null> {
    try {
      const receipt = await client.auth.operation.get({ operationId });
      const operation = receipt.operation;
      if (operation.kind === 'logout') return null;
      return operation.state !== 'authenticated' && operation.state !== 'pending';
    } catch {
      return null;
    }
  }

  async function cancelAccount(): Promise<AuthCancelResult> {
    const operationId = lastOperationId;
    const refusal = cancelRefusal();
    if (refusal) return { ok: false, cancelled: false, refusal };
    if (!operationId) return { ok: true, cancelled: false };
    reconciledLogin = null;
    unreconciledLoginId = null;
    try {
      const result = await client.auth.operation.cancel({ operationId });
      // The receipt decides: a late poll that already authenticated wins.
      const cancelled = await receiptCancelled(operationId);
      return { ok: result.ok, cancelled: cancelled ?? result.cancelled };
    } catch {
      return { ok: false, cancelled: false };
    }
  }

  async function logoutAccount(): Promise<AccountLogoutResult> {
    const refusal = refused(access, 'auth.logout');
    if (refusal) return { status: 'unknown', refusal };
    reconciledLogin = null;
    unreconciledLoginId = null;
    let operationId: string;
    try {
      const accepted = await client.auth.logout({ confirm: true, operationKey: newOperationKey() });
      operationId = accepted.operationId;
      if (accepted.state === 'failed') {
        return { status: 'failed', message: '退出未完成，请重新打开 Hyprial。', removed: accepted.removed, nextAction: accepted.nextAction };
      }
      if (accepted.state === 'unknown') {
        return { status: 'unknown', message: '退出结果未知，请刷新账号状态。', removed: accepted.removed, nextAction: accepted.nextAction };
      }
      if (accepted.loggedOut) {
        return { status: 'logged-out', message: '本机登录已确认退出。', removed: accepted.removed, nextAction: accepted.nextAction };
      }
    } catch (error) {
      const lost = error instanceof ClientError && error.dispatch === 'unknown';
      if (lost) {
        return { status: 'unknown', message: '退出请求结果未知；请刷新账号状态后再重试。' };
      }
      if (error instanceof ClientError) {
        const removed = removedLabels(error);
        return {
          status: 'failed',
          message:
            removed === undefined
              ? `退出请求未被接受（${error.code}）。`
              : `部分登录信息已移除（${error.code}），请重新打开 Hyprial。`,
          code: error.code,
          ...(removed === undefined ? {} : { removed, nextAction: 'inspect' as const }),
        };
      }
      return { status: 'failed', message: '退出请求未被接受。' };
    }
    // accepted != completed: poll the receipt, never navigate optimistically.
    const deadlineAt = Date.now() + LOGIN_BUDGET_MS;
    while (Date.now() < deadlineAt) {
      try {
        const receipt = await client.auth.operation.get({ operationId });
        const operation = receipt.operation;
        if (operation.kind === 'logout') {
          if (operation.state === 'completed') {
            // ``failure`` is a strict, secret-free server reason; when it is
            // null the exit was clean. Never invert that polarity.
            return {
              status: 'logged-out',
              removed: operation.removed,
              nextAction: 'login',
              message: operation.failure
                ? `本机登录已退出；服务端报告异常：${operation.failure.reason}，请刷新状态。`
                : '本机登录已确认退出。',
            };
          }
          if (operation.state === 'failed') {
            return { status: 'failed', message: '退出未完成，请重新打开 Hyprial。',
              removed: operation.removed, nextAction: 'inspect',
              ...(operation.failure ? { code: operation.failure.reason } : {}) };
          }
          if (operation.state === 'unknown') {
            return { status: 'unknown', message: '退出结果未知；请刷新账号状态。', removed: operation.removed, nextAction: 'inspect' };
          }
        }
      } catch {
        return { status: 'unknown', message: '退出结果未知；请刷新账号状态。' };
      }
      await sleep(POLL_INTERVAL_MS);
    }
    return { status: 'unknown', message: '退出操作已受理，但未在期限内确认完成。' };
  }

  const actions = Object.freeze({
    getAccountState: readStatus,
    readLoginOperation,
    authenticateAccount,
    cancelAccount,
    cancelRefusal,
    loginRefusal,
    logoutAccount,
    subscribeAccountProgress(listener: (progress: AccountProgress) => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  });

  return Object.freeze({
    actions,
    busy: () => inFlight.current !== null,
    operationId: () => lastOperationId,
  });
}
