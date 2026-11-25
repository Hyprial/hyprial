import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../support/demo-baseline';
import { App } from '../../src/App';
import { LiveError, type LivePort } from '../../src/adapters/live';

function port(): LivePort {
  return { call: vi.fn(async (operation, params = {}) => {
    if (operation === 'targets.list') return { targets: [{ targetUri: 'agent:test@b:node:Live Agent', nodeId: 'node', status: 'online' }], preferredTargetUri: 'agent:test@b:node:Live Agent' };
    if (operation === 'sessions.pending') return { requests: [] };
    if (operation === 'sessions.create') return { sessionId: 's1', conversationId: 'c1', targetUri: params.targetUri };
    if (operation === 'sessions.history') return { events: [{ eventId: 'real-1', kind: 'assistant', text: 'durable reply', at: '2026-10-29T00:00:00Z' }], nextCursor: null };
    if (operation === 'lark.adapters') return { adapters: [] };
    if (operation === 'sessions.send') throw new LiveError('transport', 'lost response');
    if (operation === 'sessions.send.lookup') return { status: 'unknown', reason: 'not-found' };
    throw new Error(operation);
  }) };
}
it('connects the official chat navigation to authoritative history and unknown-result lookup', async () => {
  const client = port(); render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={client} baseline={DEMO_BASELINE} />);
  fireEvent.click(screen.getByRole('button', { name: /^聊天/ }));
  expect(await screen.findByText('durable reply')).toBeInTheDocument();
  const input = screen.getByRole('textbox', { name: '给 agent:test@b:node:Live Agent 的消息' });
  fireEvent.change(input, { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '查询原消息结果' })).toBeInTheDocument());
  expect(input).toHaveValue('hello');
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '查询原消息结果' }));
  await waitFor(() => expect(client.call).toHaveBeenCalledWith('sessions.send.lookup', expect.any(Object)));
});
it('has no Agent 设置 entry when the live host has no credential or platform port', async () => {
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={port()} initialPage="connections" />);
  fireEvent.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await waitFor(() => expect(screen.getByRole('menuitem', { name: '用户设置' })).toBeInTheDocument());
  expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
  expect(screen.queryByRole('heading', { name: '凭据设置' })).toBeNull();
  expect(screen.queryByRole('heading', { name: '平台接入' })).toBeNull();
  expect(document.querySelector('.page-connections')).toBeNull();
});
it('does not enable the legacy live bridge from production mode', () => {
  vi.stubEnv('PROD', true);
  const request = vi.fn();
  vi.stubGlobal('fetch', request);
  try {
    render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} />); fireEvent.click(screen.getByRole('button', { name: /^聊天/ }));
    expect(screen.queryByText('正在连接聊天服务…')).not.toBeInTheDocument();
    expect(screen.queryByText(/本机服务/)).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  } finally { vi.unstubAllGlobals(); vi.unstubAllEnvs(); }
});
it('does not enable the legacy live bridge from a compiled flag', () => {
  vi.stubGlobal('__HYPRIAL_LIVE_ENABLED__', true);
  const request = vi.fn();
  vi.stubGlobal('fetch', request);
  try {
    render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} />); fireEvent.click(screen.getByRole('button', { name: /^聊天/ }));
    expect(screen.queryByText('正在连接聊天服务…')).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  } finally { vi.unstubAllGlobals(); vi.unstubAllEnvs(); }
});
