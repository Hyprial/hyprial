import { expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DomainError, HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

function setup() {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'test-only' });
  let exists = false;
  let unavailable = false;
  const grant = { adapter: 'demo', appId: 'cli_test', state: 'unauthorized', capabilities: [], missingCapabilities: ['message-send'], checkedAt: 1700000000000 };
  const operation = { ok: true, operationId: 'op-test', adapter: 'demo', provider: 'lark', kind: 'authorization', status: 'awaiting_user',
    verificationUrl: 'https://accounts.feishu.cn/authorize', expiresAt: Date.now() + 600000, authorization: grant };
  const invoke = vi.spyOn(transport, 'invoke').mockImplementation(async method => {
    if (method === 'adapter.list') return { ok: true, adapters: [{ name: 'demo', provider: 'lark', configured: true, desired: false, processRunning: false, status: 'stopped', online: false, agent: null }] };
    if (method === 'adapter.identities.list') return { ok: true, adapter: 'demo', count: 0, identities: [] };
    if (method === 'adapter.authorization.status') return { ok: true, ...grant };
    if (method === 'adapter.authorization.operation') {
      if (!exists) throw new DomainError('ADAPTER_OPERATION_NOT_FOUND', 'not found', null, { method, requestId: null, status: 400, dispatch: 'dispatched', catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 1 });
      return operation;
    }
    if (method === 'adapter.authorization.begin') {
      exists = true;
      if (unavailable) throw new Error('response lost');
      return operation;
    }
    if (method === 'adapter.authorization.cancel') return { ...operation, status: 'cancelled', verificationUrl: undefined, expiresAt: undefined };
    throw new Error('unexpected');
  });
  const api = createPlatformIntegrationsApiFromClient(createGeneratedClient(transport));
  return { api, invoke, loseReply: () => { unavailable = true; }, restore: () => { exists = true; } };
}

it('uses declared authorization methods with explicit minimal consent and no write replay', async () => {
  const { api, invoke } = setup();
  expect(api.capabilities.authorization.available).toBe(true);
  await api.beginAuthorization!('demo');
  expect(invoke).toHaveBeenCalledWith('adapter.authorization.begin', expect.objectContaining({
    name: 'demo', operationKey: expect.any(String), capabilities: ['message-send', 'receive-p2p', 'receive-group-mention'],
  }), expect.anything());
  await api.authorizationOperation!('op-test');
  await api.cancelAuthorization!('op-test');
  expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.authorization.begin', 'adapter.authorization.operation', 'adapter.authorization.cancel']);
});

it('starts authorization with a UUID v4 when randomUUID is unavailable on HTTP', async () => {
  const { api, invoke } = setup();
  const getRandomValues = crypto.getRandomValues.bind(crypto);
  vi.stubGlobal('crypto', { getRandomValues });
  try {
    await api.beginAuthorization!('demo');
    const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(invoke).toHaveBeenCalledWith('adapter.authorization.begin', expect.objectContaining({
      name: 'demo', operationKey: expect.stringMatching(uuidV4),
    }), expect.anything());
  } finally {
    vi.unstubAllGlobals();
  }
});

it('restores an existing operation after opening details without another begin', async () => {
  const { api, invoke, restore } = setup();
  restore();
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  await user.click(await screen.findByRole('button', { name: '详情' }));
  expect(await screen.findByRole('link', { name: '打开平台授权页面' })).toHaveAttribute('href', 'https://accounts.feishu.cn/authorize');
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.authorization.begin')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '取消授权' }));
  expect(await screen.findByText('已取消')).toBeVisible();
  expect(screen.queryByRole('link', { name: '打开平台授权页面' })).toBeNull();
});

it('blocks another begin after a lost response and refreshes the same daemon operation', async () => {
  const { api, invoke, loseReply } = setup();
  loseReply();
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  await user.click(await screen.findByRole('button', { name: '详情' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '授权' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '授权' }));
  expect(await screen.findByText('结果未确认')).toBeVisible();
  expect(screen.queryByRole('button', { name: '授权' })).toBeNull();
  await user.click(screen.getByRole('button', { name: '刷新授权状态' }));
  expect(await screen.findByRole('link', { name: '打开平台授权页面' })).toBeVisible();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.authorization.begin')).toHaveLength(1);
});
