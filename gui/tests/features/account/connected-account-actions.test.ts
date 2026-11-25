/**
 * A7 connected account actions: GeneratedClient-only, receipt-driven.
 *
 * These tests pin the review semantics: one in-flight operation (double mount
 * dedupe), absolute expiry, unknown never auto-retried, cancel/late-poll decided
 * by the server receipt, receipt restore after reload, no token in browser
 * storage, all five actions through the GeneratedClient, and register refused at
 * every layer.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DomainError, TransportError, type MethodAccess } from '../../../src/client';
import {
  createConnectedAccountPort,
  projectAccountStatus,
} from '../../../src/features/account/impl/connected-account-actions';
import type { GeneratedClient } from '../../../src/client/generated';
import type { OnboardingStatusResult } from '../../../src/client/generated/types';

type AccountClient = Pick<GeneratedClient, 'auth' | 'onboarding'>;
const asClient = (client: FakeClient) => client as unknown as AccountClient;
const asStatus = (value: object) => value as unknown as OnboardingStatusResult;

const OWNER = 'a7-owner@agents.hyprial.local';

function diagnostics(method: string, dispatch: 'not-dispatched' | 'dispatched' | 'unknown') {
  return {
    method,
    requestId: null,
    status: null,
    dispatch,
    catalogDigest: null,
    attempts: 1,
    startedAt: 0,
    durationMs: 0,
  } as const;
}

function allowAll(): MethodAccess {
  return () => ({ available: true });
}

interface FakeClient {
  onboarding: { status: ReturnType<typeof vi.fn> };
  auth: {
    login: { begin: ReturnType<typeof vi.fn> };
    operation: { get: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> };
    relogin: ReturnType<typeof vi.fn>;
    logout: ReturnType<typeof vi.fn>;
  };
}

function fakeClient(overrides: Partial<FakeClient> = {}): FakeClient {
  const client: FakeClient = {
    onboarding: {
      status: vi.fn(async () => ({
        ok: true, authenticated: false, deviceReady: true, initialized: true,
        nextActions: [], operations: [], owner: null, phase: 'serving',
      })),
    },
    auth: {
      login: { begin: vi.fn() },
      operation: { get: vi.fn(), cancel: vi.fn() },
      relogin: vi.fn(),
      logout: vi.fn(),
    },
  };
  return { ...client, ...overrides, auth: { ...client.auth, ...(overrides.auth ?? {}) } };
}

const signedOut = {
  ok: true, authenticated: false, deviceReady: true, initialized: true,
  nextActions: [], operations: [], owner: null, phase: 'serving' as const,
};

function loginReceipt(
  state: 'pending' | 'authenticated' | 'cancelled',
  operationId = 'op-1',
  verification?: {
    readonly verificationUri: string;
    readonly userCode: string;
    readonly expiresIn: number;
    readonly expiresAt: string;
    readonly intervalSeconds: number;
  },
) {
  return {
    ok: true,
    operation: {
      kind: 'login' as const,
      operationId,
      owner: state === 'authenticated' ? OWNER : null,
      receiptId: state === 'authenticated' ? 'receipt-1' : null,
      state,
      verification: verification ?? { expiresAt: new Date(Date.now() + 300_000).toISOString(), intervalSeconds: 5, userCode: 'CODE', verificationUri: 'https://auth.example/device' },
    },
  };
}

describe('connected account port', () => {
  it('distinguishes a failed status read from a pending login receipt', async () => {
    const client = fakeClient();
    client.onboarding.status.mockRejectedValue(new TransportError('network', 'NETWORK_ERROR', 'lost', diagnostics('onboarding.status', 'unknown')));
    const port = createConnectedAccountPort(asClient(client), allowAll());
    expect((await port.actions.getAccountState!()).account).toEqual({ status: 'unavailable', message: '无法连接本机服务，请重新打开 Hyprial。' });
    expect(client.auth.login.begin).not.toHaveBeenCalled();
  });
  beforeEach(() => {
    vi.useFakeTimers();
    // System time advances with the fake clock, so the absolute deadline is real.
    vi.setSystemTime(new Date(1_000_000));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('dedupes a double mount into one begin and reuses the in-flight promise', async () => {
    const client = fakeClient();
    let finish!: (value: unknown) => void;
    client.auth.login.begin.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    client.auth.operation.get.mockImplementation(async () => loginReceipt('authenticated'));
    const port = createConnectedAccountPort(asClient(client), allowAll());

    const first = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    const second = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(second).toBe(first);
    finish({ ok: true, operationId: 'op-1', intent: 'login', owner: null, state: 'pending', verification: null });
    await vi.advanceTimersByTimeAsync(0);
    await expect(first).resolves.toMatchObject({ status: 'authenticated', owner: OWNER });
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
    expect(port.busy()).toBe(false);
  });

  it('stops at one absolute expiry instead of accumulating intervals', async () => {
    const client = fakeClient();
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-1', intent: 'login', owner: null, state: 'pending', verification: null,
    });
    client.auth.operation.get.mockImplementation(async () => loginReceipt('pending'));
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const result = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await vi.advanceTimersByTimeAsync(181_000);
    await expect(result).resolves.toMatchObject({ status: 'unknown' });
    expect(client.auth.operation.get.mock.calls.length).toBeGreaterThan(1);
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
  });

  it('never re-sends an unknown begin and mints no second key', async () => {
    const client = fakeClient();
    client.auth.login.begin.mockRejectedValue(
      new TransportError('network', 'SUBMIT_OUTCOME_UNKNOWN', 'lost', diagnostics('auth.login.begin', 'unknown')),
    );
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const result = await port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(result).toMatchObject({ status: 'unknown' });
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
  });

  it('lets the server receipt win a cancel race with a late poll', async () => {
    const client = fakeClient();
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-1', intent: 'login', owner: null, state: 'pending', verification: null,
    });
    // The login already authenticated; the cancel must not report success.
    client.auth.operation.get.mockImplementation(async () => loginReceipt('authenticated'));
    client.auth.operation.cancel.mockResolvedValue({ ok: true, cancelled: true });
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const login = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await vi.advanceTimersByTimeAsync(10);
    const cancelled = await port.actions.cancelAccount!();
    expect(cancelled).toEqual({ ok: true, cancelled: false });
    await expect(login).resolves.toMatchObject({ status: 'authenticated' });
  });

  it('restores only from the status receipt and reports pending work as unknown', () => {
    const pending = projectAccountStatus(asStatus({
      ...signedOut,
      operations: [{ kind: 'login', operationId: 'op-9', receiptId: null, state: 'pending' }],
    }));
    expect(pending.account.status).toBe('unknown');
    expect(pending.account.status).not.toBe('authenticated');

    const restored = projectAccountStatus(asStatus({
      ...signedOut, authenticated: true, owner: OWNER, operations: [],
    }));
    expect(restored).toMatchObject({ account: { status: 'authenticated', owner: OWNER } });
  });

  it('writes no token to browser storage while the operation runs', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const client = fakeClient();
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-1', intent: 'login', owner: null, state: 'pending', verification: null,
    });
    client.auth.operation.get.mockImplementation(async () => loginReceipt('authenticated'));
    const port = createConnectedAccountPort(asClient(client), allowAll());
    await port.actions.authenticateAccount!({ account: 'ignored', token: 'ignored' }, 'login');
    expect(setItem).not.toHaveBeenCalled();
    expect(globalThis.localStorage.getItem('token')).toBeNull();
  });

  it('uses the GeneratedClient for status, login, poll, relogin, cancel and logout', async () => {
    const client = fakeClient();
    client.onboarding.status
      .mockResolvedValueOnce(signedOut)
      .mockResolvedValue({ ...signedOut, authenticated: true, owner: OWNER });
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-1', intent: 'login', owner: null, state: 'pending', verification: null,
    });
    client.auth.operation.get.mockImplementation(async () => loginReceipt('authenticated'));
    client.auth.operation.cancel.mockResolvedValue({ ok: true, cancelled: true });
    client.auth.relogin.mockResolvedValue({
      ok: true, operationId: 'op-2', intent: 'login', owner: null, state: 'pending', verification: null,
    });
    client.auth.logout.mockResolvedValue({
      ok: true, operationId: 'op-3', receiptId: 'r3', loggedOut: true, issuerRevocation: 'not-attempted',
      nextAction: 'login', removed: [], state: 'completed',
    });
    const port = createConnectedAccountPort(asClient(client), allowAll());

    await port.actions.getAccountState?.();
    await port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await port.actions.cancelAccount!();
    // A reloaded status says this home is authenticated; the next login is a
    // same-owner relogin on the same GeneratedClient.
    await port.actions.getAccountState?.();
    await port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await expect(port.actions.logoutAccount?.()).resolves.toMatchObject({ status: 'logged-out' });

    expect(client.onboarding.status).toHaveBeenCalled();
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
    expect(client.auth.login.begin).toHaveBeenCalledWith(
      { intent: 'login', operationKey: expect.any(String), until: 'serving' },
      expect.any(Object),
    );
    expect(client.auth.operation.get).toHaveBeenCalled();
    expect(client.auth.operation.cancel).toHaveBeenCalledWith({ operationId: 'op-1' });
    expect(client.auth.relogin).toHaveBeenCalledTimes(1);
    expect(client.auth.relogin).toHaveBeenCalledWith(
      { operationKey: expect.any(String), until: 'serving' },
      expect.any(Object),
    );
    expect(client.auth.logout).toHaveBeenCalledWith({ confirm: true, operationKey: expect.any(String) });
  });

  it('sends until:serving on both begin paths and keeps auth.operation.get read-only across device stages', async () => {
    const client = fakeClient();
    client.onboarding.status
      .mockResolvedValueOnce(signedOut)
      .mockResolvedValue({ ...signedOut, authenticated: true, owner: OWNER });
    client.auth.login.begin.mockResolvedValue({
      ok: true,
      operationId: 'op-1',
      intent: 'login',
      owner: null,
      state: 'pending',
      verification: null,
      completion: { stage: 'authorizing', identity: null, device: null, failure: null },
    });
    client.auth.relogin.mockResolvedValue({
      ok: true,
      operationId: 'op-2',
      intent: 'login',
      owner: null,
      state: 'pending',
      verification: null,
      completion: { stage: 'authorizing', identity: null, device: null, failure: null },
    });
    client.auth.operation.get
      .mockResolvedValueOnce({
        ok: true,
        operation: {
          kind: 'login',
          operationId: 'op-1',
          owner: OWNER,
          receiptId: 'r1',
          state: 'authenticated',
          verification: null,
          completion: { stage: 'device', identity: null, device: null, failure: null },
        },
      })
      .mockResolvedValueOnce({
        ok: true,
        operation: {
          kind: 'login',
          operationId: 'op-1',
          owner: OWNER,
          receiptId: 'r1',
          state: 'authenticated',
          verification: null,
          completion: {
            stage: 'serving',
            identity: { mode: 'casdoor', issuer: 'https://auth.example', source: null },
            device: { ready: false, deviceId: null, keyGeneration: null, userKey: { state: 'unavailable', code: 'USER_KEY_ABSENT' }, error: { code: 'DEVICE_ENSURE_FAILED', message: 'failed' }, retry: 'serving-start' },
            failure: null,
          },
        },
      })
      .mockResolvedValueOnce({
        ok: true,
        operation: {
          kind: 'login',
          operationId: 'op-2',
          owner: OWNER,
          receiptId: 'r2',
          state: 'authenticated',
          verification: null,
          completion: {
            stage: 'serving',
            identity: { mode: 'casdoor', issuer: 'https://auth.example', source: null },
            device: { ready: true, deviceId: 'dev-1', keyGeneration: 1, userKey: { state: 'unbound', code: 'USER_KEY_UNBOUND' }, error: null, retry: null },
            failure: null,
          },
        },
      });
    const port = createConnectedAccountPort(asClient(client), allowAll());

    await port.actions.getAccountState?.();
    const firstPromise = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await vi.advanceTimersByTimeAsync(2_100);
    await expect(firstPromise).resolves.toEqual({ status: 'authenticated', owner: OWNER });

    await port.actions.getAccountState?.();
    await expect(
      port.actions.authenticateAccount!({ account: '', token: '' }, 'login'),
    ).resolves.toEqual({ status: 'authenticated', owner: OWNER });

    for (const [params] of client.auth.operation.get.mock.calls) {
      expect(params).not.toHaveProperty('waitSeconds');
    }
    expect(client.auth.operation.get).toHaveBeenCalledTimes(3);
  });

  it('refuses register without dispatching anything', async () => {
    const client = fakeClient();
    const port = createConnectedAccountPort(asClient(client), allowAll());
    await expect(port.actions.authenticateAccount!({ account: '', token: '' }, 'register'))
      .resolves.toMatchObject({ status: 'unavailable' });
    expect(client.auth.login.begin).not.toHaveBeenCalled();
    expect(client.auth.relogin).not.toHaveBeenCalled();
  });

  it('refuses a method the served catalog does not admit', async () => {
    const client = fakeClient();
    const access: MethodAccess = (method) => method === 'auth.login.begin'
      ? { available: false, code: 'METHOD_NOT_FOUND', reason: 'auth.login.begin 不可用' }
      : { available: true };
    const port = createConnectedAccountPort(asClient(client), access);
    const result = await port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(result).toEqual({ status: 'unavailable', refusal: { code: 'METHOD_NOT_FOUND', source: 'auth.login.begin' } });
    expect(client.auth.login.begin).not.toHaveBeenCalled();
  });
});

describe('connected account logout receipt', () => {
  const accepted = {
    ok: true, operationId: 'op-3', receiptId: 'r3', loggedOut: false,
    issuerRevocation: 'not-attempted', nextAction: 'poll', removed: [], state: 'accepted',
  } as const;
  const completed = (failure: null | { reason: string }) => ({
    ok: true,
    operation: {
      kind: 'logout' as const, operationId: 'op-3', owner: null, receiptId: 'r3',
      removed: [], state: 'completed' as const, failure,
    },
  });

  it('surfaces the server reason instead of inverting its polarity', async () => {
    const client = fakeClient();
    client.auth.logout.mockResolvedValue(accepted);
    client.auth.operation.get.mockImplementation(async () => completed({ reason: 'removal-failed' }));
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const result = await port.actions.logoutAccount!();
    expect(result.status).toBe('logged-out');
    expect(result.message).toContain('removal-failed');
    expect(result.message).toContain('异常');
  });

  it('reports a clean completed logout without inventing a reason', async () => {
    const client = fakeClient();
    client.auth.logout.mockResolvedValue(accepted);
    client.auth.operation.get.mockImplementation(async () => completed(null));
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const result = await port.actions.logoutAccount!();
    expect(result.status).toBe('logged-out');
    expect(result.message).toBe('本机登录已确认退出。');
    expect(result.message).not.toContain('异常');
    expect(result.message).not.toContain('未给出');
  });
});

describe('F5 polling follows the public interval and expiry', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000_000));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('polls once at begin, then only after the published interval', async () => {
    const client = fakeClient();
    const verification = {
      verificationUri: 'https://issuer.example.test/device',
      userCode: 'ABCD-EFGH',
      expiresIn: 1,
      // The public schedule: 30s interval, one second of validity.
      intervalSeconds: 30,
      expiresAt: new Date(1_001_000).toISOString(),
    };
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-9', intent: 'login', owner: null,
      state: 'pending', verification,
    });
    client.auth.operation.get.mockImplementation(async () => loginReceipt('pending', 'op-9', verification));
    const port = createConnectedAccountPort(asClient(client), allowAll());

    const result = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await vi.advanceTimersByTimeAsync(2_000);

    // One immediate poll; the 30s interval never elapses before expiry, so the
    // worker must not hammer the issuer again.
    expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
    await expect(result).resolves.toMatchObject({ status: 'unknown' });
    expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
  });

  it('stops polling at the receipt expiry, not the local budget', async () => {
    const client = fakeClient();
    const verification = {
      verificationUri: 'https://issuer.example.test/device',
      userCode: 'ABCD-EFGH',
      expiresIn: 120,
      intervalSeconds: 1,
      expiresAt: new Date(1_060_000).toISOString(),
    };
    client.auth.login.begin.mockResolvedValue({
      ok: true, operationId: 'op-10', intent: 'login', owner: null,
      state: 'pending', verification,
    });
    client.auth.operation.get.mockImplementation(async () => loginReceipt('pending', 'op-10', verification));
    const port = createConnectedAccountPort(asClient(client), allowAll());

    const result = port.actions.authenticateAccount!({ account: '', token: '' }, 'login');
    await vi.advanceTimersByTimeAsync(200_000);

    await expect(result).resolves.toMatchObject({ status: 'unknown' });
    const callsAtExpiry = client.auth.operation.get.mock.calls.length;
    // 60s of validity at 1s intervals: bounded by the expiry, not by 180s.
    expect(callsAtExpiry).toBeLessThanOrEqual(61);
    await vi.advanceTimersByTimeAsync(200_000);
    expect(client.auth.operation.get).toHaveBeenCalledTimes(callsAtExpiry);
  });
});

describe('F6 an unknown login receipt keeps its reconciliation context', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000_000));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('projects a persisted unknown receipt as unknown, not unauthenticated', () => {
    const snapshot = projectAccountStatus(asStatus({
      ok: true, authenticated: false, deviceReady: false, initialized: false,
      nextActions: ['login'], owner: null, phase: 'bootstrap',
      operations: [{ operationId: 'op-unknown', kind: 'login', state: 'unknown' }],
    }));

    expect(snapshot.account.status).toBe('unknown');
    expect(snapshot.account.message).toBe('未收到登录结果，请刷新账号状态。');
  });

  it('refuses to open a second login while a receipt is unreconciled', async () => {
    const client = fakeClient();
    client.onboarding.status.mockResolvedValue({
      ok: true, authenticated: false, deviceReady: false, initialized: false,
      nextActions: ['login'], owner: null, phase: 'bootstrap',
      operations: [{ operationId: 'op-unknown', kind: 'login', state: 'unknown' }],
    });
    const port = createConnectedAccountPort(asClient(client), allowAll());

    expect((await port.actions.getAccountState!()).account.status).toBe('unknown');

    const retry = await port.actions.authenticateAccount!({ account: '', token: '' }, 'login');

    expect(retry.status).toBe('unknown');
    expect(retry.message).toContain('op-unknown');
    expect(client.auth.login.begin).not.toHaveBeenCalled();
  });
});

describe('F8 a partial removal keeps its code and labels', () => {
  it.each(['initial', 'polled'])('preserves an HTTP-200 failed %s logout receipt', async source => {
    const client = fakeClient();
    const receipt = { state: 'failed', removed: ['settings.owner'], nextAction: 'inspect', loggedOut: false, operationId: 'logout-1' };
    client.auth.logout.mockResolvedValue(source === 'initial' ? receipt : { ...receipt, state: 'accepted', nextAction: 'poll', removed: [] });
    client.auth.operation.get.mockResolvedValue({ ok: true, operation: { ...receipt, kind: 'logout', failure: { reason: 'removal-failed' } } });
    const port = createConnectedAccountPort(asClient(client), allowAll());
    const result = await port.actions.logoutAccount!();
    expect(result).toMatchObject({ status: 'failed', removed: ['settings.owner'], nextAction: 'inspect' });
    expect(result.message).toContain('重新打开 Hyprial');
    if (source === 'initial') expect(client.auth.operation.get).not.toHaveBeenCalled();
    else expect(result.code).toBe('removal-failed');
    expect(client.auth.logout).toHaveBeenCalledOnce();
  });

  it('reports PARTIAL_REMOVAL with the artifacts the server removed', async () => {
    const client = fakeClient();
    client.auth.logout.mockRejectedValue(
      new DomainError(
        'PARTIAL_REMOVAL',
        'credential removal failed after settings were rewritten',
        { cause: 'PERMISSION_DENIED', removed: ['settings.owner', 'settings.userId'] },
        diagnostics('auth.logout', 'dispatched'),
      ),
    );
    const port = createConnectedAccountPort(asClient(client), allowAll());

    const result = await port.actions.logoutAccount!();

    expect(result.status).toBe('failed');
    expect(result.code).toBe('PARTIAL_REMOVAL');
    expect(result.removed).toEqual(['settings.owner', 'settings.userId']);
    expect(result.message).toContain('PARTIAL_REMOVAL');
    expect(result.message).toContain('部分');
  });

  it('keeps the typed code even when no labels were reported', async () => {
    const client = fakeClient();
    client.auth.logout.mockRejectedValue(
      new DomainError('DAEMON_NOT_READY', 'no custody capability', {}, diagnostics('auth.logout', 'not-dispatched')),
    );
    const port = createConnectedAccountPort(asClient(client), allowAll());

    const result = await port.actions.logoutAccount!();

    expect(result.status).toBe('failed');
    expect(result.code).toBe('DAEMON_NOT_READY');
    expect(result.removed).toBeUndefined();
  });
});

it.each([
  { authenticated: true, deviceReady: true, initialized: false, phase: 'bootstrap', expected: 'not-ready' },
  { authenticated: true, deviceReady: true, initialized: false, phase: 'serving', expected: 'not-ready' },
  { authenticated: true, deviceReady: true, initialized: true, phase: 'serving', expected: 'ready' },
  { authenticated: false, deviceReady: true, initialized: true, phase: 'serving', expected: 'not-ready' },
])('requires authoritative completed initialization: %j', ({ expected, ...status }) => {
  const projected = projectAccountStatus(asStatus({ ...status, operations: [] }));
  expect(projected.device.status).toBe(expected);
  if (status.deviceReady && !status.initialized) {
    expect(projected.device.message).toBe('设备已登录，但初始化状态尚未完成。');
  }
});

it.each(['pending', 'unknown'])('keeps a %s device receipt unresolved after a status refresh', state => {
  const snapshot = projectAccountStatus(asStatus({ authenticated: true, deviceReady: false,
    initialized: false, phase: 'bootstrap', operations: [{ kind: 'device', state }] }));
  expect(snapshot.device.status).toBe('unknown');
});

it.each(['pending', 'accepted', 'promoting', 'unknown'] as const)(
  'keeps a device-ready initialize receipt in unknown state while %s', state => {
    const snapshot = projectAccountStatus(asStatus({ authenticated: true, deviceReady: true,
      initialized: false, phase: 'bootstrap', operations: [{ kind: 'initialize', state }] }));
    expect(snapshot.device.status).toBe('unknown');
    expect(snapshot.device.message).toBe('初始化结果尚未确认，请刷新状态。');
  },
);

it('status reads do not advance a pending login; its owner reconciles the receipt', async () => {
  const client = fakeClient();
  const operation = { operationId: 'expired-login', kind: 'login', state: 'pending' };
  client.onboarding.status
    .mockResolvedValueOnce({ ...signedOut, operations: [operation] })
    .mockResolvedValue({ ...signedOut, operations: [{ ...operation, state: 'expired' }] });
  client.auth.operation.get.mockResolvedValue({ ok: true, operation: { ...operation, state: 'expired' } });
  const port = createConnectedAccountPort(asClient(client), allowAll());
  const snapshot = await port.actions.getAccountState!();
  expect(client.auth.operation.get).not.toHaveBeenCalled();
  expect(snapshot.pendingLogin).toEqual({ operationId: 'expired-login' });
  const account = await port.actions.readLoginOperation!('expired-login');
  expect(client.auth.operation.get).toHaveBeenCalledWith({ operationId: 'expired-login' });
  expect(account).toMatchObject({ status: 'failed', message: '授权已过期，请重新登录。', retryable: true });
  expect(client.onboarding.status).toHaveBeenCalledTimes(1);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
});

it('allows one manual new login after authoritative expiry and never replays automatically', async () => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue({ ...signedOut, operations: [
    { operationId: 'expired-login', kind: 'login', state: 'expired' },
  ] });
  client.auth.login.begin.mockResolvedValue({ operationId: 'new-login', state: 'pending' });
  client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated', 'new-login'));
  const port = createConnectedAccountPort(asClient(client), allowAll());
  await port.actions.getAccountState!();
  expect(client.auth.login.begin).not.toHaveBeenCalled();
  expect(await port.actions.authenticateAccount!({ account: '', token: '' }, 'login')).toMatchObject({ status: 'authenticated' });
  expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
});

it.each(['pending', 'network failure'])('allows a fresh receipt read after %s without replaying login', async outcome => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue({ ...signedOut, operations: [loginReceipt('pending').operation] });
  if (outcome === 'pending') client.auth.operation.get.mockResolvedValueOnce(loginReceipt('pending'));
  else client.auth.operation.get.mockRejectedValueOnce(new Error('offline'));
  client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated'));
  const port = createConnectedAccountPort(asClient(client), allowAll());
  await port.actions.getAccountState!();

  expect(await port.actions.readLoginOperation!('op-1')).toMatchObject({ status: 'unknown' });
  expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
  expect(await port.actions.readLoginOperation!('op-1')).toEqual({ status: 'authenticated', owner: OWNER });
  expect(client.auth.operation.get).toHaveBeenCalledTimes(2);
  expect(client.onboarding.status).toHaveBeenCalledTimes(1);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
  expect(client.auth.relogin).not.toHaveBeenCalled();
});

it('rejects a late receipt from the previous login without clearing the current selector', async () => {
  const client = fakeClient();
  client.onboarding.status
    .mockResolvedValueOnce({ ...signedOut, operations: [loginReceipt('pending', 'old-login').operation] })
    .mockResolvedValue({ ...signedOut, operations: [loginReceipt('pending', 'new-login').operation] });
  let finish!: (value: ReturnType<typeof loginReceipt>) => void;
  client.auth.operation.get
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValue(loginReceipt('authenticated', 'new-login'));
  const port = createConnectedAccountPort(asClient(client), allowAll());
  await port.actions.getAccountState!();
  const old = port.actions.readLoginOperation!('old-login');
  await port.actions.getAccountState!();

  finish(loginReceipt('authenticated', 'old-login'));
  expect(await old).toMatchObject({ status: 'unknown' });
  expect(await port.actions.readLoginOperation!('new-login')).toEqual({ status: 'authenticated', owner: OWNER });
  expect(client.auth.operation.get.mock.calls.map(([input]) => input.operationId)).toEqual(['old-login', 'new-login']);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
  expect(client.auth.relogin).not.toHaveBeenCalled();
});

it('keeps a terminal receipt available to delayed consumers of the same status observation', async () => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue({ ...signedOut, operations: [loginReceipt('pending').operation] });
  client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated'));
  const port = createConnectedAccountPort(asClient(client), allowAll());
  await port.actions.getAccountState!();
  expect(await port.actions.readLoginOperation!('op-1')).toEqual({ status: 'authenticated', owner: OWNER });
  expect(await port.actions.readLoginOperation!('op-1')).toEqual({ status: 'authenticated', owner: OWNER });
  expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
});

it.each(['signed-out status', 'failed status', 'logout'])('invalidates a terminal login receipt after %s', async transition => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValueOnce({ ...signedOut, operations: [loginReceipt('pending').operation] });
  client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated'));
  client.auth.logout.mockResolvedValue({ state: 'completed', loggedOut: true, removed: [], nextAction: 'login' });
  const port = createConnectedAccountPort(asClient(client), allowAll());
  await port.actions.getAccountState!();
  expect(await port.actions.readLoginOperation!('op-1')).toMatchObject({ status: 'authenticated' });

  if (transition === 'logout') await port.actions.logoutAccount!();
  else {
    if (transition === 'failed status') client.onboarding.status.mockRejectedValue(new Error('offline'));
    else client.onboarding.status.mockResolvedValue(signedOut);
    await port.actions.getAccountState!();
  }
  expect(await port.actions.readLoginOperation!('op-1')).toMatchObject({ status: 'unknown' });
  expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
});

it('forwards username from onboarding.status while preserving owner and surfaces failed conflict errors', async () => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue({
    ok: true,
    authenticated: true,
    owner: 'legacy-owner',
    username: 'renamed-user',
    deviceReady: true,
    initialized: true,
    nextActions: [],
    operations: [],
    phase: 'serving',
  });
  const port = createConnectedAccountPort(asClient(client), allowAll());
  const snapshot = await port.actions.getAccountState!();
  expect(snapshot.account).toEqual({
    status: 'authenticated',
    owner: 'legacy-owner',
    username: 'renamed-user',
  });

  client.onboarding.status.mockResolvedValue({
    ...signedOut,
    operations: [{ operationId: 'conflict-op', kind: 'login', state: 'pending', receiptId: null }],
  });
  const conflictMessage = '此 HYPRIAL_HOME 已绑定另一个账号并留有其数据，本次登录未写入任何内容。请换一个新的 HYPRIAL_HOME，或让 agent 按 docs/guides/migration-account-binding.md 处理后重试。';
  client.auth.operation.get.mockResolvedValue({
    ok: true,
    operation: {
      operationId: 'conflict-op',
      kind: 'login',
      state: 'failed',
      owner: null,
      receiptId: null,
      verification: null,
      error: {
        code: 'IDENTITY_CONFLICT',
        message: conflictMessage,
        data: {
          reason: 'home-bound-to-another-account',
          guide: 'docs/guides/migration-account-binding.md',
          loginUserId: 'u-other',
          issuer: 'https://id.example.invalid',
        },
      },
    },
  });
  await port.actions.getAccountState!();
  const receipt = await port.actions.readLoginOperation!('conflict-op');
  expect(receipt).toEqual({
    status: 'failed',
    message: conflictMessage,
    retryable: false,
  });
});
