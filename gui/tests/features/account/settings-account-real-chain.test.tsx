/**
 * Settings account real chain: the connected GeneratedClient port composed into
 * the settings hook, as `capabilities.tsx` does for the product page.
 *
 * Pinned here: a confirmed logout must move the displayed snapshot to the
 * status receipt (never keep the stale owner); refresh stays usable while a
 * cancellation is unconfirmed; an unknown-state retry must project an explicit
 * status instead of failing silently; relogin keeps using the same client;
 * DAEMON_NOT_READY from the unassembled logout custody shows as a typed,
 * honest failure (backend work package, recorded BLOCKED for the container).
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DomainError, type MethodAccess } from '../../../src/client';
import { createConnectedAccountPort, useAccountSettings } from '../../../src/features/account';
import { type AccountSnapshot, type FirstRunActions } from '../../../src/features/onboarding';
import type { GeneratedClient } from '../../../src/client/generated';
import type { OnboardingStatusResult } from '../../../src/client/generated/types';
import { stubFirstRunActions } from '../../support/first-run-actions';

type AccountClient = Pick<GeneratedClient, 'auth' | 'onboarding'>;
const asClient = (client: FakeClient) => client as unknown as AccountClient;
const asStatus = (value: object) => value as unknown as OnboardingStatusResult;

const OWNER = 'chain-owner@agents.hyprial.local';
const OWNER_RENEWED = 'chain-renewed@agents.hyprial.local';

const allowAll: MethodAccess = () => ({ available: true });

interface FakeClient {
  onboarding: { status: ReturnType<typeof vi.fn> };
  auth: {
    login: { begin: ReturnType<typeof vi.fn> };
    operation: { get: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> };
    relogin: ReturnType<typeof vi.fn>;
    logout: ReturnType<typeof vi.fn>;
  };
}

function fakeClient(): FakeClient {
  return {
    onboarding: { status: vi.fn() },
    auth: {
      login: { begin: vi.fn() },
      operation: { get: vi.fn(), cancel: vi.fn() },
      relogin: vi.fn(),
      logout: vi.fn(),
    },
  };
}

function statusReceipt(overrides: Partial<OnboardingStatusResult> = {}) {
  return asStatus({
    ok: true, authenticated: false, deviceReady: true, initialized: true,
    nextActions: [], operations: [], owner: null, phase: 'serving',
    ...overrides,
  });
}

const signedOut = statusReceipt();
const pendingLoginStatus = statusReceipt({
  operations: [{ kind: 'login', operationId: 'op-1', receiptId: null, state: 'pending' }],
});

function loginReceipt(
  state: 'pending' | 'authenticated' | 'cancelled',
  operationId = 'op-1',
  owner: string | null = null,
) {
  return {
    ok: true,
    operation: {
      kind: 'login' as const,
      operationId,
      owner: state === 'authenticated' ? owner : null,
      receiptId: state === 'authenticated' ? 'receipt-1' : null,
      state,
      verification: {
        expiresAt: new Date(Date.now() + 300_000).toISOString(),
        intervalSeconds: 5,
        userCode: 'CODE',
        verificationUri: 'https://auth.example/device',
      },
    },
  };
}

const beginPending = (operationId = 'op-1') => ({
  ok: true, operationId, intent: 'login', owner: null, state: 'pending', verification: null,
});

const acceptedLogout = (operationId = 'op-logout') => ({
  ok: true, operationId, receiptId: 'r-logout', loggedOut: false,
  issuerRevocation: 'not-attempted', nextAction: 'poll', removed: [], state: 'accepted',
});

const completedLogout = (failure: null | { reason: string }) => ({
  ok: true,
  operation: {
    kind: 'logout' as const, operationId: 'op-logout', owner: null, receiptId: 'r-logout',
    removed: [], state: 'completed' as const, failure,
  },
});

function dispatchDiagnostics(method: string) {
  return {
    method,
    requestId: null,
    status: null,
    dispatch: 'dispatched' as const,
    catalogDigest: null,
    attempts: 1,
    startedAt: 0,
    durationMs: 0,
  } as const;
}

function chainActions(client: FakeClient): { client: FakeClient; actions: FirstRunActions } {
  const port = createConnectedAccountPort(asClient(client), allowAll);
  return { client, actions: { ...stubFirstRunActions(), ...port.actions } };
}

async function mountedHook(actions: FirstRunActions) {
  const { result } = renderHook(() => useAccountSettings(actions));
  await act(async () => {});
  return result;
}

describe('settings account real chain (port + hook)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000_000));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('refreshes the account snapshot from the status receipt after a confirmed logout', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValueOnce(statusReceipt({ authenticated: true, owner: OWNER }))
      .mockResolvedValueOnce(signedOut);
    client.auth.logout.mockResolvedValue(acceptedLogout());
    client.auth.operation.get.mockResolvedValue(completedLogout(null));
    const result = await mountedHook(actions);
    expect(result.current.snapshot.account).toMatchObject({ status: 'authenticated', owner: OWNER });

    await act(async () => { await result.current.logout(); });

    expect(result.current.logoutState?.status).toBe('logged-out');
    expect(result.current.snapshot.account.status).toBe('unauthenticated');
    expect(client.onboarding.status.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('projects the status-read failure instead of keeping a stale authenticated display after logout', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValueOnce(statusReceipt({ authenticated: true, owner: OWNER }))
      .mockRejectedValueOnce(new Error('status read lost'));
    client.auth.logout.mockResolvedValue(acceptedLogout());
    client.auth.operation.get.mockResolvedValue(completedLogout(null));
    const result = await mountedHook(actions);

    await act(async () => { await result.current.logout(); });

    expect(result.current.logoutState?.status).toBe('logged-out');
    expect(result.current.snapshot.account.status).toBe('unavailable');
    expect(result.current.snapshot.account).not.toMatchObject({ owner: OWNER });
  });

  it('projects unknown when the post-logout status read rejects', async () => {
    const getAccountState = vi.fn()
      .mockResolvedValueOnce({ account: { status: 'authenticated', owner: OWNER }, device: { status: 'ready' } })
      .mockRejectedValueOnce(new Error('status read lost'));
    const actions: FirstRunActions = {
      ...stubFirstRunActions(),
      getAccountState,
      logoutAccount: async () => ({ status: 'logged-out', message: '本机登录已确认退出。' }),
    };
    const result = await mountedHook(actions);
    expect(result.current.snapshot.account).toMatchObject({ status: 'authenticated', owner: OWNER });

    await act(async () => { await result.current.logout(); });

    expect(result.current.logoutState?.status).toBe('logged-out');
    expect(result.current.snapshot.account.status).toBe('unknown');
  });

  it('keeps refresh available while a cancellation is unconfirmed and re-reads the receipt', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValueOnce(signedOut)
      .mockResolvedValue(pendingLoginStatus);
    client.auth.login.begin.mockResolvedValue(beginPending());
    client.auth.operation.get.mockResolvedValue(loginReceipt('pending'));
    client.auth.operation.cancel.mockResolvedValue({ ok: true, cancelled: true, operationId: 'op-1', reason: 'outcome-unknown', state: 'pending' });
    const result = await mountedHook(actions);
    expect(result.current.snapshot.account.status).toBe('unauthenticated');

    act(() => { void result.current.run('login'); });
    await act(async () => {});
    const confirmed = await act(async () => result.current.cancel());
    expect(confirmed).toBe(false);
    const readsAfterCancel = client.onboarding.status.mock.calls.length;

    await act(async () => { await result.current.run('read'); });

    expect(client.onboarding.status.mock.calls.length).toBeGreaterThan(readsAfterCancel);
    expect(result.current.snapshot.account.status).toBe('unknown');
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
  });

  it('projects an explicit unavailable status when the unknown-state retry read fails', async () => {
    const authenticateAccount = vi.fn(async () => { throw new Error('begin lost'); });
    const getAccountState = vi.fn()
      .mockResolvedValueOnce({ account: { status: 'unauthenticated' }, device: { status: 'not-ready' } })
      .mockRejectedValueOnce(new Error('status read lost'));
    const actions: FirstRunActions = {
      ...stubFirstRunActions(),
      authenticateAccount,
      getAccountState,
    };
    const result = await mountedHook(actions);
    act(() => { void result.current.run('login'); });
    await act(async () => {});
    expect(result.current.snapshot.account.status).toBe('unknown');

    await act(async () => { void result.current.retry(); });
    await act(async () => {});

    expect(authenticateAccount).toHaveBeenCalledTimes(1);
    expect(result.current.snapshot.account.status).toBe('unavailable');
    expect(result.current.snapshot.account.message).toContain('账号服务不可用');
  });

  it('falls through to the login action when no status read exists for an unknown state', async () => {
    const authenticateAccount = vi.fn(async () => ({ status: 'failed' as const, message: '授权未完成。', retryable: true }));
    const actions = stubFirstRunActions({ authenticateAccount });
    const result = await mountedHook(actions);
    expect(result.current.snapshot.account.status).toBe('unknown');

    await act(async () => { void result.current.retry(); });
    await act(async () => {});

    expect(authenticateAccount).toHaveBeenCalledTimes(1);
    expect(result.current.snapshot.account.status).toBe('failed');
  });

  it('drives the login display through the one GeneratedClient port', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValueOnce(signedOut)
      .mockResolvedValue(statusReceipt({ authenticated: true, owner: OWNER }));
    client.auth.login.begin.mockResolvedValue(beginPending());
    client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated', 'op-1', OWNER));
    const result = await mountedHook(actions);

    await act(async () => { await result.current.run('login'); });

    expect(result.current.snapshot.account).toMatchObject({ status: 'authenticated', owner: OWNER });
    expect(client.auth.login.begin).toHaveBeenCalledTimes(1);
    expect(client.auth.operation.get).toHaveBeenCalled();
  });

  it('relogins through the same GeneratedClient when the home is already authenticated', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValueOnce(statusReceipt({ authenticated: true, owner: OWNER }))
      .mockResolvedValueOnce(statusReceipt({ authenticated: true, owner: OWNER_RENEWED }));
    client.auth.relogin.mockResolvedValue({ ...beginPending('op-2'), operationId: 'op-2' });
    client.auth.operation.get.mockResolvedValue(loginReceipt('authenticated', 'op-2', OWNER_RENEWED));
    const result = await mountedHook(actions);

    await act(async () => { await result.current.run('login'); });

    expect(client.auth.relogin).toHaveBeenCalledTimes(1);
    expect(client.auth.login.begin).not.toHaveBeenCalled();
    expect(result.current.snapshot.account).toMatchObject({ status: 'authenticated', owner: OWNER_RENEWED });
  });

  it('shows the DAEMON_NOT_READY logout refusal as a typed failed result without inventing success', async () => {
    const { client, actions } = chainActions(fakeClient());
    client.onboarding.status
      .mockResolvedValue(statusReceipt({ authenticated: true, owner: OWNER }));
    client.auth.logout.mockRejectedValue(new DomainError(
      'DAEMON_NOT_READY',
      'no logout custody capability is registered for this home and phase',
      {},
      dispatchDiagnostics('auth.logout'),
    ));
    const result = await mountedHook(actions);

    await act(async () => { await result.current.logout(); });

    expect(result.current.logoutState?.status).toBe('failed');
    expect(result.current.logoutState?.code).toBe('DAEMON_NOT_READY');
    expect(result.current.logoutState?.message).toContain('DAEMON_NOT_READY');
  });
});

describe('snapshot projections used by the settings account section', () => {
  it('keeps the snapshot and logout state when logout is refused without dispatch', async () => {
    const getAccountState = vi.fn(async () =>
      ({ account: { status: 'authenticated', owner: OWNER }, device: { status: 'ready' } }) satisfies AccountSnapshot);
    const actions: FirstRunActions = {
      ...stubFirstRunActions(),
      getAccountState,
      logoutAccount: async () => ({ status: 'unknown', refusal: { code: 'METHOD_NOT_FOUND', source: 'auth.logout' } }),
    };
    const notify = vi.fn();
    const { result } = renderHook(() => useAccountSettings(actions, notify));
    await act(async () => {});

    await act(async () => { await result.current.logout(); });

    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      tone: 'warning', text: '退出本机登录暂不可用。', source: 'auth.logout · METHOD_NOT_FOUND',
    }));
    expect(result.current.logoutState).toBeNull();
    expect(result.current.snapshot.account.status).toBe('authenticated');
    expect(result.current.canLogout).toBe(true);
  });
});

it.each([
  ['authenticated', 'authenticated'], ['expired', 'failed'], ['denied', 'failed'], ['unknown', 'unknown'],
])('reconciles restored %s login in its controller without a second status read', async (state, expected) => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue(pendingLoginStatus);
  client.auth.operation.get.mockResolvedValue({ ok: true, operation: {
    kind: 'login', operationId: 'op-1', state, owner: OWNER, verification: null,
  } });
  const port = createConnectedAccountPort(asClient(client), allowAll);
  const actions = { ...stubFirstRunActions(), ...port.actions };
  const { result } = renderHook(() => useAccountSettings(actions));
  await waitFor(() => expect(client.auth.operation.get).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(result.current.snapshot.account.status).toBe(expected));
  expect(result.current.snapshot.device.status).toBe('not-ready');
  expect(client.onboarding.status).toHaveBeenCalledTimes(1);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
  expect(client.auth.relogin).not.toHaveBeenCalled();
});

it.each(['refresh', 'remount'])('keeps the authenticated receipt when %s overlaps reconciliation', async overlap => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue(pendingLoginStatus);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  client.auth.operation.get.mockImplementation(async () => {
    await pending;
    return loginReceipt('authenticated', 'op-1', OWNER);
  });
  const port = createConnectedAccountPort(asClient(client), allowAll);
  const actions = { ...stubFirstRunActions(), ...port.actions };
  let hook = renderHook(() => useAccountSettings(actions));
  await waitFor(() => expect(client.auth.operation.get).toHaveBeenCalledTimes(1));

  if (overlap === 'refresh') {
    await act(async () => { await hook.result.current.run('read'); });
  } else {
    hook.unmount();
    hook = renderHook(() => useAccountSettings(actions));
  }
  await waitFor(() => expect(client.onboarding.status).toHaveBeenCalledTimes(2));
  await act(async () => { release(); await pending; });

  await waitFor(() => expect(hook.result.current.snapshot.account).toEqual({ status: 'authenticated', owner: OWNER }));
  expect(hook.result.current.snapshot.pendingLogin).toBeUndefined();
  expect(hook.result.current.snapshot.device.status).toBe('not-ready');
  expect(client.auth.operation.get).toHaveBeenCalledTimes(1);
  expect(client.auth.login.begin).not.toHaveBeenCalled();
  expect(client.auth.relogin).not.toHaveBeenCalled();
});

it.each(['run', 'retry'] as const)('keeps the verify-previous-attempt answer over a login refusal when %s meets an unreconciled receipt', async entry => {
  const client = fakeClient();
  client.onboarding.status.mockResolvedValue(pendingLoginStatus);
  client.auth.operation.get.mockResolvedValue(loginReceipt('pending'));
  const refusing: MethodAccess = method => method === 'auth.login.begin'
    ? { available: false, code: 'METHOD_NOT_FOUND' } : { available: true };
  const port = createConnectedAccountPort(asClient(client), refusing);
  const actions = { ...stubFirstRunActions(), ...port.actions };
  const notify = vi.fn();
  const { result } = renderHook(() => useAccountSettings(actions, notify));
  await waitFor(() => expect(result.current.snapshot.pendingLogin).toBeDefined());
  await waitFor(() => expect(client.auth.operation.get).toHaveBeenCalled());
  await act(async () => { await (entry === 'run' ? result.current.run('login') : result.current.retry()); });
  expect(notify).not.toHaveBeenCalled();
  expect(client.auth.login.begin).not.toHaveBeenCalled();
});
