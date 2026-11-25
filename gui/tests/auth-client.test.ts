import { describe, expect, it, vi } from 'vitest';
import { createNativeAuthClient, type AuthClient } from '../src/platform/auth';

const state = { authenticated: true, owner: 'test@example.test', deviceReady: false, sidecarError: 'SIDECAR_MISSING', ready: false };
describe('native authentication client', () => {
  it('native adapter only delegates to an explicitly supplied capability', async () => {
    const capability = { status: vi.fn().mockResolvedValue(state), login: vi.fn().mockResolvedValue({ ok: true, state }), device: vi.fn(), cancel: vi.fn() };
    expect(await createNativeAuthClient(capability).status()).toEqual(state);
    expect(capability.status).toHaveBeenCalledOnce();
    expect(createNativeAuthClient(capability).open).toBeUndefined();
  });
  it('forwards only the fixed native open acknowledgement', async () => {
    const open = vi.fn(async () => ({
      ok: true,
      opened: true,
      verificationUri: 'https://auth.hyprial.com/device?code=1',
      access_token: 'SECRET',
    }));
    const client = createNativeAuthClient({
      status: vi.fn(async () => state),
      login: vi.fn(async () => ({ ok: true, state })),
      device: vi.fn(async () => ({ ok: true })),
      cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
      open,
    });
    const result = await client.open?.();
    expect(result).toEqual({ ok: true, opened: true });
    expect(JSON.stringify(result)).not.toMatch(/auth\.hyprial|SECRET|verificationUri|access_token/);
    expect(open).toHaveBeenCalledOnce();
  });
  it('fails closed for native open while retaining only safe retry metadata', async () => {
    const withOpen = (open: NonNullable<AuthClient['open']>) => createNativeAuthClient({
      status: vi.fn(async () => state),
      login: vi.fn(async () => ({ ok: true, state })),
      device: vi.fn(async () => ({ ok: true })),
      cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
      open,
    });
    const failed = withOpen(async () => ({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_FAILED',
      retryable: true,
      verificationUri: 'https://auth.hyprial.com/device',
    }));
    expect(await failed.open?.()).toEqual({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_FAILED',
      retryable: true,
    });

    const unavailable = withOpen(async () => ({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_UNAVAILABLE',
      retryable: false,
    }));
    expect(await unavailable.open?.()).toEqual({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_UNAVAILABLE',
      retryable: false,
    });

    for (const value of [
      { ok: true },
      { ok: true, opened: 'true' },
      { ok: false, opened: false, code: 'not a code', retryable: true },
      { ok: false, opened: false, code: 'AUTH_FAILED', retryable: 'true' },
      null,
      undefined,
    ]) {
      const malformed = withOpen(async () => value as unknown as Awaited<ReturnType<NonNullable<AuthClient['open']>>>);
      expect(await malformed.open?.()).toEqual({ ok: false, opened: false });
    }

    const thrown = withOpen(async () => {
      const error = new Error('raw opener detail https://auth.hyprial.com/device SECRET');
      Object.assign(error, { code: 'DESKTOP_ACCOUNT_OPEN_FAILED', retryable: true });
      throw error;
    });
    const result = await thrown.open?.();
    expect(result).toEqual({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_FAILED',
      retryable: true,
    });
    expect(JSON.stringify(result)).not.toMatch(/raw opener|auth\.hyprial|SECRET/);
  });
  it('passes through only an explicit native cancel acknowledgement', async () => {
    const adapter = (value: unknown) => createNativeAuthClient({
      status: vi.fn(async () => state),
      login: vi.fn(async () => ({ ok: true, state })),
      device: vi.fn(async () => ({ ok: true })),
      cancel: vi.fn(async () => value) as unknown as AuthClient['cancel'],
    }).cancel();
    expect(await adapter({ ok: true, cancelled: true })).toEqual({ ok: true, cancelled: true });
    expect(await adapter({ ok: true, cancelled: false })).toEqual({ ok: true, cancelled: false });
    for (const value of [
      { ok: false, cancelled: true },
      { ok: false, cancelled: false },
      { ok: true },
      { cancelled: true },
      { ok: 'true', cancelled: true },
      {},
      null,
      undefined,
      'ok',
    ]) {
      expect(await adapter(value)).toEqual({ ok: false, cancelled: false });
    }
    const rejected = createNativeAuthClient({
      status: vi.fn(async () => state),
      login: vi.fn(async () => ({ ok: true, state })),
      device: vi.fn(async () => ({ ok: true })),
      cancel: vi.fn(async () => { throw new Error('connection lost'); }),
    });
    expect(await rejected.cancel()).toEqual({ ok: false, cancelled: false });
  });
});
