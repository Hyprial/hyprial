import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DomainError, HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

function setup(mode: 'stopped' | 'running' | 'unknown' | 'stale' | 'readback' = 'stopped') {
  let removed = false;
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic-only' });
  const invoke = vi.spyOn(transport, 'invoke').mockImplementation(async method => {
    if (method === 'adapter.list') return { ok: true, adapters: removed ? [] : [{
      id: 'lark:demo', provider: 'lark', name: 'demo', status: 'stopped', online: false,
      configured: true, desired: false, processRunning: false, agent: 'worker',
    }] };
    if (method === 'management.adapter.remove.preview') return {
      ok: true, name: 'demo', provider: 'lark', operationId: 'confirmed-demo', expiresAt: 300000,
      summary: 'Remove local app', boundAgent: 'worker', processRunning: mode === 'running',
      routes: ['team'], credentialRemoved: true,
    };
    if (method === 'management.adapter.remove') {
      if (mode === 'unknown') throw new Error(mode);
      if (mode === 'stale') throw new DomainError('PREVIEW_STALE', 'changed', null, {
        method, requestId: 'test', status: 409, catalogDigest: null, attempts: 1,
        startedAt: 0, durationMs: 1, dispatch: 'dispatched',
      });
      removed = mode !== 'readback';
      return { ok: true, name: 'demo', provider: 'lark', removed: true, unboundAgent: 'worker', credentialRemoved: true };
    }
    if (method === 'adapter.authorization.status') return { ok: true, adapter: 'demo', state: 'unknown', capabilities: [], missingCapabilities: [], appId: 'cli_test', checkedAt: null };
    if (method === 'adapter.identities.list') return { ok: true, identities: [] };
    throw new Error('Unexpected method ' + method);
  });
  const api = createPlatformIntegrationsApiFromClient(createGeneratedClient(transport));
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  return { user, invoke, api };
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('demo');
  await user.click(screen.getByRole('button', { name: '详情' }));
  await user.click(screen.getByRole('button', { name: '删除接入' }));
  return screen.findByRole('dialog');
}

it('sends only the preview operation ID and confirms deletion from a new list read', async () => {
  const { user, invoke } = setup();
  const dialog = await open(user);
  expect(within(dialog).getByText('绑定 Agent：worker')).toBeVisible();
  expect(within(dialog).getByText('同时移除消息路由：team')).toBeVisible();
  await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
  expect(await screen.findByText('暂无平台接入')).toBeVisible();
  expect(invoke).toHaveBeenCalledWith('management.adapter.remove', { operationId: 'confirmed-demo' }, { retry: 'never' });
  expect(invoke.mock.calls.filter(([method]) => method === 'management.adapter.remove')).toHaveLength(1);
  const methods = invoke.mock.calls.map(([method]) => method);
  expect(methods.at(-1)).toBe('adapter.list');
});

it('cancellation has no removal call', async () => {
  const { user, invoke } = setup();
  const dialog = await open(user);
  await user.click(within(dialog).getByRole('button', { name: '取消' }));
  expect(invoke.mock.calls.some(([method]) => method === 'management.adapter.remove')).toBe(false);
  expect(screen.getByText('demo')).toBeVisible();
});

it('requires the running platform to stop before confirmation', async () => {
  const { user, invoke } = setup('running');
  const dialog = await open(user);
  expect(within(dialog).getByText('请先停止该平台接入。')).toBeVisible();
  expect(within(dialog).getByRole('button', { name: '确认删除' })).toBeDisabled();
  expect(invoke.mock.calls.some(([method]) => method === 'management.adapter.remove')).toBe(false);
});

it.each(['unknown', 'stale', 'readback'] as const)('holds %s outcomes until a new preview', async mode => {
  const { user, invoke } = setup(mode);
  const dialog = await open(user);
  await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
  await within(dialog).findByText(mode === 'stale' ? /删除预览已失效/ : /删除结果尚未确认/);
  expect(within(dialog).getByRole('button', { name: '确认删除' })).toBeDisabled();
  await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(invoke.mock.calls.filter(([method]) => method === 'management.adapter.remove')).toHaveLength(1));
  expect(screen.queryByText('平台接入已删除。')).toBeNull();
});
