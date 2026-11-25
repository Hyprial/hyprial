import { describe, expect, it, vi } from 'vitest';
import { createAccountActions } from '../src/features/onboarding/impl/auth-actions';
import type { AuthClient, AuthState } from '../src/platform/auth';
import { SetupError, type SetupClient, type SetupResult } from '../src/platform/setup';

const state: AuthState = {
  authenticated: true, owner: 'person@example.invalid', deviceReady: false,
  sidecarError: 'SIDECAR_MISSING', ready: false,
};
function client(overrides: Partial<AuthClient> = {}): AuthClient {
  return {
    status: vi.fn(async () => state),
    login: vi.fn(async () => ({ ok: true, state })),
    device: vi.fn(async () => ({ ok: false, state })),
    cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
    ...overrides,
  };
}
function setup(overrides: Partial<SetupClient> = {}): SetupClient {
  return {
    status: vi.fn(async () => ({ ok: true, keySaved: false, modelVerified: false as const })),
    saveModelKey: vi.fn(async () => ({ ok: true, saved: true, modelVerified: false as const })),
    ...overrides,
  };
}

describe('account-only onboarding actions', () => {
  it('keeps successful login distinct from a missing device', async () => {
    const host = client();
    const actions = createAccountActions(host);
    expect(await actions.authenticateAccount!({ account: '', token: '' }, 'login')).toMatchObject({ status: 'authenticated' });
    expect(await actions.getAccountState?.()).toMatchObject({
      account: { status: 'authenticated', owner: state.owner },
      device: { status: 'failed' },
    });
    expect(host.login).toHaveBeenCalledTimes(1);
    await actions.initializeDevice!();
    expect(host.login).toHaveBeenCalledTimes(1);
    expect(host.device).toHaveBeenCalledTimes(1);
  });

  it('restores signed-out state without starting authorization', async () => {
    const host = client({ status: async () => ({ ...state, authenticated: false, owner: null, sidecarError: null }) });
    expect(await createAccountActions(host).getAccountState?.()).toEqual({
      account: { status: 'unauthenticated' }, device: { status: 'not-ready' },
    });
    expect(host.login).not.toHaveBeenCalled();
  });

  it('forwards public progress and cancellation without sending supplied legacy credentials', async () => {
    const host = client({ login: vi.fn(async (notify) => {
      notify?.({ event: 'device', verificationUri: 'https://auth.example.invalid', userCode: 'TEST' });
      notify?.({ event: 'unknown' });
      return { ok: false };
    }) });
    const actions = createAccountActions(host);
    const progress = vi.fn();
    const unsubscribe = actions.subscribeAccountProgress?.(progress);
    await actions.authenticateAccount!({ account: 'not-used', token: 'not-transmitted' }, 'login');
    expect(host.login).toHaveBeenCalledWith(expect.any(Function));
    expect(progress).toHaveBeenCalledWith({ event: 'device', verificationUri: 'https://auth.example.invalid', userCode: 'TEST' });
    expect(progress).toHaveBeenCalledWith({ event: 'unknown' });
    unsubscribe?.();
    await actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(progress).toHaveBeenCalledTimes(2);
    expect(await actions.cancelAccount?.()).toEqual({ ok: true, cancelled: true });
    expect(host.cancel).toHaveBeenCalledTimes(1);
  });

  it('exposes only generic key/status ports without prepareSquire or squireState', async () => {
    const actions = createAccountActions(client(), setup({
      status: vi.fn(async () => ({
        ok: true,
        keySaved: true,
        pendingAction: null,
        squireState: 'name-frozen' as unknown as undefined,
        modelVerified: false as const,
      }) as unknown as SetupResult),
    }));
    expect('prepareSquire' in actions).toBe(false);
    expect('cancelPreparation' in actions).toBe(false);
    const snapshot = await actions.getSetupState?.();
    expect(snapshot).toEqual({ keySaved: true, pendingAction: null });
    expect(snapshot).not.toHaveProperty('squireState');
    expect(snapshot).not.toHaveProperty('created');
    expect(snapshot).not.toHaveProperty('granted');
    expect(snapshot).not.toHaveProperty('started');
  });

  it('keeps a save whose response was lost unknown rather than unavailable', async () => {
    const saveModelKey = vi.fn(async () => { throw new Error('private socket detail'); });
    const result = await createAccountActions(client(), setup({ saveModelKey })).saveModelKey?.('test-key');
    expect(result).toMatchObject({ status: 'unknown' });
    expect(result?.message).toContain('保存请求可能已发出');
    expect(JSON.stringify(result)).not.toContain('private socket detail');
  });

  it('keeps a malformed successful save response unknown rather than failed', async () => {
    const malformed = { ok: true, modelVerified: false as const } as unknown as SetupResult;
    const result = await createAccountActions(client(), setup({
      saveModelKey: vi.fn(async () => malformed),
    })).saveModelKey?.('test-key');
    expect(result).toMatchObject({ status: 'unknown' });
    expect(result?.message).toContain('保存请求可能已发出');
  });

  it('preserves explicit rejection and unavailable-capability outcomes', async () => {
    const rejected = await createAccountActions(client(), setup({
      saveModelKey: vi.fn(async () => ({ ok: false, code: 'MODEL_KEY_REQUIRED', modelVerified: false as const })),
    })).saveModelKey?.('test-key');
    expect(rejected).toMatchObject({ status: 'failed' });
    expect(rejected?.message).toContain('重新输入并保存');

    const unavailable = await createAccountActions(client(), setup({
      saveModelKey: vi.fn(async () => { throw new SetupError('SETUP_UNAVAILABLE'); }),
    })).saveModelKey?.('test-key');
    expect(unavailable).toMatchObject({ status: 'unavailable' });

    const protocol = await createAccountActions(client(), setup({
      saveModelKey: vi.fn(async () => { throw new SetupError('SETUP_PROTOCOL_ERROR'); }),
    })).saveModelKey?.('test-key');
    expect(protocol).toMatchObject({ status: 'unknown' });
  });
});
