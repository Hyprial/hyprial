import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useAccountSettings } from '../src/features/account';
import { useFirstRunController } from '../src/features/onboarding/impl/useFirstRunController';
import type { AccountSnapshot, FirstRunActions } from '../src/features/onboarding/impl/types';
import type { AuthCancelResult } from '../src/platform/auth';
import { stubFirstRunActions } from './support/first-run-actions';

const signedOut: AccountSnapshot = {
  account: { status: 'unauthenticated' },
  device: { status: 'not-ready' },
};
const alreadyAuthenticated: AccountSnapshot = {
  account: { status: 'authenticated', owner: 'already@example.invalid' },
  device: { status: 'ready' },
};
const confirmed: AuthCancelResult = { ok: true, cancelled: true };
const notCancelled: AuthCancelResult = { ok: true, cancelled: false };
const failed: AuthCancelResult = { ok: false, cancelled: true };

type CancelAction = NonNullable<FirstRunActions['cancelAccount']>;

function pendingActions(overrides: Partial<FirstRunActions> = {}): FirstRunActions {
  return {
    ...stubFirstRunActions(),
    authenticateAccount: () => new Promise<never>(() => {}),
    getAccountState: async () => signedOut,
    ...overrides,
  };
}
function useSettings(actions: FirstRunActions) {
  const state = useAccountSettings(actions);
  return { start: () => { void state.run('login'); }, retry: () => { void state.retry(); } };
}
function useOnboarding(actions: FirstRunActions) {
  const state = useFirstRunController({ actions });
  return { start: state.runAccount, retry: state.retryAccount };
}
async function startAndRetry(useSurface: typeof useSettings, actions: FirstRunActions) {
  const { result } = renderHook(() => useSurface(actions));
  await act(async () => {});
  act(() => { result.current.start(); });
  await act(async () => { result.current.retry(); });
  return result;
}
async function startAndRetryAgain(result: { current: { start: () => void; retry: () => void } }) {
  act(() => { result.current.start(); });
  await act(async () => { result.current.retry(); });
}

for (const [surface, useSurface] of [['settings', useSettings], ['onboarding', useOnboarding]] as const) {
  describe(`${surface} cancellation acknowledgement`, () => {
    for (const failure of ['cancel rejects', 'cancel missing'] as const) {
      it(`does not start another login when ${failure}, even though status can be refreshed`, async () => {
        const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
        const actions = pendingActions({
          authenticateAccount,
          cancelAccount: failure === 'cancel rejects'
            ? vi.fn(async () => { throw new Error('connection lost'); })
            : undefined,
        });
        const result = await startAndRetry(useSurface, actions);
        await startAndRetryAgain(result);
        expect(authenticateAccount).toHaveBeenCalledTimes(1);
      });
    }

    it('does not start another login when the status refresh after cancel is unconfirmed', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const actions = pendingActions({
        authenticateAccount,
        cancelAccount: vi.fn(async () => confirmed),
        getAccountState: vi.fn(async () => { throw new Error('status unknown'); }),
      });
      const result = await startAndRetry(useSurface, actions);
      await startAndRetryAgain(result);
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
    });

    it('does not start another login when the native cancel result says not cancelled', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => notCancelled);
      const result = await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount }));
      await startAndRetryAgain(result);
      expect(cancelAccount).toHaveBeenCalledTimes(2);
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
    });

    it('does not start another login when the native cancel result reports failure', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => failed);
      const result = await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount }));
      await startAndRetryAgain(result);
      expect(cancelAccount).toHaveBeenCalledTimes(2);
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
    });

    for (const [shape, value] of [
      ['missing', undefined],
      ['null', null],
      ['empty', {}],
      ['without cancelled', { ok: true }],
      ['non-boolean', { ok: 'true', cancelled: true }],
    ] as const) {
      it(`does not start another login when the native cancel result is ${shape}`, async () => {
        const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
        const cancelAccount = vi.fn(async () => value) as unknown as CancelAction;
        const result = await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount }));
        await startAndRetryAgain(result);
        expect(authenticateAccount).toHaveBeenCalledTimes(1);
      });
    }

    it('does not start a second login after cancellation refreshes to an already ready account', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => confirmed);
      let reads = 0;
      const getAccountState = vi.fn(async () => (++reads === 1 ? signedOut : alreadyAuthenticated));
      await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount, getAccountState }));
      expect(cancelAccount).toHaveBeenCalledTimes(1);
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
    });

    it('allows retry after cancellation and state reconciliation succeed', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => confirmed);
      await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount }));
      expect(cancelAccount).toHaveBeenCalledTimes(1);
      expect(authenticateAccount).toHaveBeenCalledTimes(2);
    });

    it('retries login only after a later cancel and status refresh both succeed', async () => {
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      let attempts = 0;
      const cancelAccount = vi.fn(async () => {
        attempts += 1;
        return attempts === 1 ? failed : confirmed;
      });
      const result = await startAndRetry(useSurface, pendingActions({ authenticateAccount, cancelAccount }));
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
      await startAndRetryAgain(result);
      expect(cancelAccount).toHaveBeenCalledTimes(2);
      expect(authenticateAccount).toHaveBeenCalledTimes(2);
    });
  });
}

describe('A7 account lifecycle without implicit server cancellation', () => {
  it('stops polling on unmount and never cancels the server operation', async () => {
    const cancelAccount = vi.fn(async () => confirmed);
    const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
    const actions = pendingActions({ authenticateAccount, cancelAccount });
    const { result, unmount } = renderHook(() => useAccountSettings(actions));
    await act(async () => {});
    act(() => { void result.current.run('login'); });
    await act(async () => {});
    unmount();
    expect(cancelAccount).not.toHaveBeenCalled();
    expect(authenticateAccount).toHaveBeenCalledTimes(1);
  });

  it('re-reads status for an unknown receipt and does not begin again', async () => {
    const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
    const getAccountState = vi.fn(async () => ({
      account: { status: 'unknown' as const, message: '授权操作 op-9 仍在进行；请刷新以读取服务端回执。' },
      device: { status: 'not-ready' as const },
    }));
    const actions = pendingActions({ authenticateAccount, getAccountState });
    const { result } = renderHook(() => useAccountSettings(actions));
    await act(async () => {});
    await act(async () => { void result.current.retry(); });
    expect(authenticateAccount).not.toHaveBeenCalled();
    expect(getAccountState.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(result.current.snapshot.account.status).toBe('unknown');
  });
});
