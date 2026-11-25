import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App, type AppProps } from '../src/App';
import { createGuiCapabilities } from '../src/app/capabilities';
import type { AuthClient, AuthState } from '../src/platform/auth';
import {
  SetupError,
  createNativeSetupClient,
  type SetupClient,
  type SetupResult,
} from '../src/platform/setup';

const READY: SetupResult = {
  ok: true,
  keySaved: true,
  pendingAction: null,
  modelVerified: false,
};

function host(overrides: Partial<SetupClient> = {}): SetupClient {
  return {
    status: vi.fn(async (): Promise<SetupResult> => ({
      ok: true,
      keySaved: false,
      pendingAction: 'save',
      modelVerified: false,
    })),
    saveModelKey: vi.fn(async () => ({ ...READY, saved: true })),
    ...overrides,
  };
}

function auth(): AuthClient {
  const state: AuthState = {
    authenticated: true,
    owner: 'ready@example.invalid',
    deviceReady: true,
    sidecarError: null,
    ready: true,
  };
  return {
    status: vi.fn(async () => state),
    login: vi.fn(async () => ({ ok: true, state })),
    device: vi.fn(async () => ({ ok: true, state })),
    cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
  };
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__hyprialAccount;
  delete (window as unknown as Record<string, unknown>).__hyprialSetup;
  delete (globalThis as unknown as Record<string, unknown>).indexedDB;
});

function storageText(storage: Storage): string {
  const rows: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null) rows.push(`${key}=${storage.getItem(key) ?? ''}`);
  }
  return rows.join('\n');
}

describe('native setup capability', () => {
  it('forwards status and save over the injected bridge without prepare or cancel', async () => {
    const capability = host();
    const setup = createNativeSetupClient(capability);
    expect(await setup.status()).toEqual({ ok: true, keySaved: false, pendingAction: 'save', modelVerified: false });

    const key = 'sk-isolated-test-value';
    expect(await setup.saveModelKey(key)).toMatchObject({ ok: true, saved: true, keySaved: true });
    expect(capability.saveModelKey).toHaveBeenCalledTimes(1);
    expect(capability.saveModelKey).toHaveBeenCalledWith(key);
    expect('prepareSquire' in setup).toBe(false);
    expect('cancel' in setup).toBe(false);
  });

  it('never persists the model key in browser storage and ignores legacy Squire fields', async () => {
    const key = 'sk-do-not-store-anywhere';
    const setup = createNativeSetupClient(host({
      status: vi.fn(async () => ({
        ...READY,
        squireState: 'destroyed',
        created: true,
        granted: true,
        started: true,
      } as unknown as SetupResult)),
    }));
    await setup.saveModelKey(key);
    const snapshot = await setup.status();

    expect(snapshot).not.toHaveProperty('squireState');
    expect(snapshot).not.toHaveProperty('created');
    expect(snapshot).not.toHaveProperty('granted');
    expect(snapshot).not.toHaveProperty('started');
    expect(storageText(localStorage)).not.toContain(key);
    expect(storageText(sessionStorage)).not.toContain(key);
  });

  it('fails closed on refusal, malformed answers, and unsafe host errors', async () => {
    await expect(createNativeSetupClient(host({
      status: vi.fn(async () => ({ ok: false, code: 'SETUP_BUSY' } as SetupResult)),
    })).status()).rejects.toMatchObject({ code: 'SETUP_BUSY' });

    await expect(createNativeSetupClient(host({
      status: vi.fn(async () => null as unknown as SetupResult),
    })).status()).rejects.toBeInstanceOf(SetupError);

    await expect(createNativeSetupClient(host({
      status: vi.fn(async () => {
        throw Object.assign(new Error('host detail'), { code: 'not-a-code' });
      }),
    })).status()).rejects.toMatchObject({ code: 'SETUP_UNAVAILABLE' });
  });

  it('keeps ambiguous native save failures unknown through the shared actions', async () => {
    const save = async (overrides: Partial<SetupClient>) =>
      createGuiCapabilities(auth(), host(overrides)).firstRun.saveModelKey?.('isolated-key');

    const generic = await save({ saveModelKey: vi.fn(async () => {
      throw new Error('private host detail');
    }) });
    expect(generic).toMatchObject({ status: 'unknown' });
    expect(generic?.message).toContain('保存请求可能已发出');
    expect(JSON.stringify(generic)).not.toContain('private host detail');

    const failed = await save({ saveModelKey: vi.fn(async () => ({
      ok: false, code: 'SETUP_FAILED', modelVerified: false as const,
    })) });
    expect(failed).toMatchObject({ status: 'unknown' });

    const cancelled = await save({ saveModelKey: vi.fn(async () => ({
      ok: false, code: 'SETUP_CANCELLED', modelVerified: false as const,
    })) });
    expect(cancelled).toMatchObject({ status: 'unknown' });

    const malformed = await save({ saveModelKey: vi.fn(async () => null as unknown as SetupResult) });
    expect(malformed).toMatchObject({ status: 'unknown' });
  });

  it('preserves native save refusals that occur before the credential write', async () => {
    const save = async (overrides: Partial<SetupClient>) =>
      createGuiCapabilities(auth(), host(overrides)).firstRun.saveModelKey?.('isolated-key');

    const rejected = await save({ saveModelKey: vi.fn(async () => ({
      ok: false, code: 'MODEL_KEY_ALREADY_GRANTED', modelVerified: false as const,
    })) });
    expect(rejected).toMatchObject({ status: 'failed' });

    const unavailable = await save({ saveModelKey: vi.fn(async () => {
      throw Object.assign(new Error('host detail'), { code: 'SETUP_UNAVAILABLE' });
    }) });
    expect(unavailable).toMatchObject({ status: 'unavailable' });
    expect(JSON.stringify(unavailable)).not.toContain('host detail');
  });

  it('enables the shared setup actions only when both native bridges exist', async () => {
    const setup = host();
    const capabilities = createGuiCapabilities(auth(), setup);
    expect(capabilities.firstRun.getSetupState).toBeDefined();
    expect(await capabilities.firstRun.getSetupState?.()).toEqual({
      keySaved: false,
      pendingAction: 'save',
    });
    expect(await capabilities.firstRun.saveModelKey?.('sk-isolated-test-value'))
      .toMatchObject({ status: 'saved' });
    expect(setup.saveModelKey).toHaveBeenCalledTimes(1);

    expect(createGuiCapabilities(auth()).firstRun.getSetupState).toBeUndefined();
  });

  it('uses explicitly supplied native capabilities in the shared four-step first-run flow without SquireStep', async () => {
    let keySaved = false;
    const account = auth();
    const setup = host({
      status: vi.fn(async (): Promise<SetupResult> => ({
        ok: true,
        keySaved,
        pendingAction: null,
        modelVerified: false,
      })),
      saveModelKey: vi.fn(async () => {
        keySaved = true;
        return { ...READY, saved: true };
      }),
    });
    const indexedDbAccess = vi.fn();
    Object.defineProperty(globalThis, 'indexedDB', {
      configurable: true,
      value: new Proxy({}, {
        get: (_target, property) => {
          indexedDbAccess(property);
          return undefined;
        },
      }),
    });
    const user = userEvent.setup();
    render(createElement<AppProps>(App, {
      initialPage: 'first-run',
      capabilities: createGuiCapabilities(account, setup),
    }));

    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));

    const key = 'sk-app-entry-isolated-only';
    await user.type(screen.getByLabelText(/DeepSeek API Key/), key);
    await user.click(screen.getByRole('button', { name: '保存并继续' }));

    expect(await screen.findByRole('heading', { name: '首次对话' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();
    expect(account.status).toHaveBeenCalled();
    expect(setup.status).toHaveBeenCalled();
    expect(setup.saveModelKey).toHaveBeenCalledWith(key);
    expect(storageText(localStorage)).not.toContain(key);
    expect(storageText(sessionStorage)).not.toContain(key);
    expect(indexedDbAccess).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(key);
  });

});
