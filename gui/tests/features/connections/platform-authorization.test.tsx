import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

function setup() {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'test-only' });
  const result = {
    ok: true, adapter: 'demo', appId: 'cli_test', state: 'unauthorized',
    capabilities: ['message-send'], missingCapabilities: ['receive-group-mention'], checkedAt: 1700000000250,
  };
  const invoke = vi.spyOn(transport, 'invoke').mockImplementation(async method => {
    if (method === 'adapter.list') return { ok: true, adapters: [{
      name: 'demo', provider: 'lark', configured: true, desired: false, processRunning: false, status: 'stopped', online: false, agent: null,
    }] };
    if (method === 'adapter.authorization.status') return result;
    if (method === 'adapter.identities.list') return { ok: true, adapter: 'demo', count: 0, identities: [] };
    throw new Error('unexpected method');
  });
  const api = createPlatformIntegrationsApiFromClient(createGeneratedClient(transport));
  return { api, invoke, result };
}

it('reads authorization independently of listing without applying scopes', async () => {
  const { api, invoke } = setup();
  await api.list();
  expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.list']);
  expect(await api.authorizationStatus!('demo')).toMatchObject({ state: 'unauthorized', checkedAt: 1700000000250 });
  expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.list', 'adapter.authorization.status']);
});

it('shows observed capability IDs and clears them when the next observation fails', async () => {
  const { api, invoke } = setup();
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  const list = await screen.findByRole('list', { name: '平台接入列表' });
  await user.click(within(list).getByRole('button', { name: '详情' }));
  expect(await screen.findByText('message-send')).toBeVisible();
  expect(screen.getByText('receive-group-mention')).toBeVisible();
  expect(screen.getByText('cli_test')).toBeVisible();
  invoke.mockRejectedValue(new Error('platform unavailable'));
  await user.click(screen.getByRole('button', { name: '刷新权限' }));
  expect(await screen.findByText('权限读取失败。')).toBeVisible();
  expect(screen.queryByText('message-send')).toBeNull();
  expect(screen.queryByText('receive-group-mention')).toBeNull();
  expect(screen.queryByText('暂无已授权权限')).toBeNull();
});
