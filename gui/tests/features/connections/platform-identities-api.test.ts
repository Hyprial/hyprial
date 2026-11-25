import { describe, expect, it, vi } from 'vitest';
import { createGeneratedClient } from '../../../src/client/generated';
import type { AdapterIdentitiesSyncResult } from '../../../src/client/generated/types';
import { HttpTransport } from '../../../src/client';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

const report = {
  ok: false, adapter: 'main', appId: 'cli_example', bot: { openId: 'ou_bot', appName: 'App' },
  chats: [], chatsComplete: false, identitiesRecorded: 2, errors: ['chats: enumeration-incomplete (budget-exhausted)'],
};

function setup(result: unknown) {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'test-only' });
  const invoke = vi.spyOn(transport, 'invoke').mockResolvedValue(result);
  const client = createGeneratedClient(transport);
  return { client, invoke, api: createPlatformIntegrationsApiFromClient(client) };
}

describe('platform identity daemon requests', () => {
  it('reads the selected adapter once without running sync or extra list calls', async () => {
    const result = { ok: true, adapter: 'main', count: 0, identities: [] };
    const { api, invoke } = setup(result);
    expect(api.listIdentities).toBeTypeOf('function');
    await expect(api.listIdentities!('main')).resolves.toEqual(result);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('adapter.identities.list', { name: 'main' }, { retry: 'safe' });
  });

  it('keeps partial collection partial and invokes only the declared sync method', async () => {
    const { api, invoke } = setup(report);
    expect(api.capabilities.identities.available).toBe(true);
    await expect(api.syncIdentities!('main')).resolves.toEqual(report);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('adapter.identities.sync', { name: 'main' }, { retry: 'never' });
  });

  it.each([true, false])('preserves absent platform names in a report with ok=%s', async (ok) => {
    const unnamed: AdapterIdentitiesSyncResult = {
      ...report, ok, bot: { openId: 'ou_bot', appName: null },
      chats: [{ chatId: 'oc_group', name: null, memberTotal: null, membersRecorded: 0, complete: ok }],
      chatsComplete: ok, errors: ok ? [] : report.errors,
    };
    const { api, invoke } = setup(unnamed);
    await expect(api.syncIdentities!('main')).resolves.toEqual(unnamed);
    expect(invoke).toHaveBeenCalledExactlyOnceWith('adapter.identities.sync', { name: 'main' }, { retry: 'never' });
  });

  it('refuses a disabled catalog method before dispatch', async () => {
    const { client, invoke } = setup(report);
    const api = createPlatformIntegrationsApiFromClient(client, () => ({ available: false, reason: 'SERVICE_NOT_READY' }));
    expect(api.capabilities.identities.available).toBe(false);
    await expect(api.syncIdentities!('main')).rejects.toMatchObject({ method: 'adapter.identities.sync', code: 'METHOD_NOT_AVAILABLE' });
    expect(api.capabilities.identities).toEqual({ available: false, code: 'METHOD_NOT_AVAILABLE', source: 'adapter.identities.sync' });
    expect(invoke).not.toHaveBeenCalled();
  });

  it('does not replay a lost write response', async () => {
    const { api, invoke } = setup(report);
    invoke.mockRejectedValue(new Error('response lost'));
    await expect(api.syncIdentities!('main')).rejects.toThrow('response lost');
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it('rejects results for another adapter', async () => {
    const { api } = setup({ ok: true, adapter: 'other', count: 0, identities: [] });
    await expect(api.listIdentities!('main')).rejects.toThrow();
  });
});
