import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { App } from '../../src/App';
import { LinkedPreviewComposition } from '../support/linked-composition';
import type { LivePort } from '../../src/adapters/live';
import { useLiveRuntime } from '../../src/pages/live/runtime';
import { createInitialSession } from '../../src/dev/session/initial-session';
import type { LinkedSession } from '../../src/dev/session/session-types';
import { selectAgentSeed, selectMachineModel, selectOrgModel, selectTopologyModel } from '../../src/dev/session/session-selectors';
import { selectChatSeed, selectChatTargets, selectConnections, selectFirstRunState } from '../../src/dev/session/session-selectors';
import { DEMO_BASELINE, AUTHENTICATED_TEST_CAPABILITIES } from '../support/demo-baseline';
import type { StateSeed } from '../../src/state/types';

it('does not create a legacy HTTP connection when no live capability is supplied', () => {
  const fetcher = vi.fn().mockRejectedValue(new Error('unexpected transport'));
  vi.stubGlobal('fetch', fetcher);
  try {
    const { result, unmount } = renderHook(() => useLiveRuntime(true));
    expect(result.current).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
    unmount();
  } finally { vi.unstubAllGlobals(); }
});

function backend(): LivePort {
  return { call: vi.fn(async (operation, params = {}) => {
    if (operation === 'targets.list') return { targets: [{ targetUri: 'agent:test@b:node:Live Agent', status: 'online' }], preferredTargetUri: 'agent:test@b:node:Live Agent' };
    if (operation === 'sessions.pending') return { requests: [] };
    if (operation === 'sessions.create') return { sessionId: 's', conversationId: 'c', targetUri: params.targetUri };
    if (operation === 'sessions.history') return { events: [{ eventId: 'real', kind: 'assistant', text: 'verified host reply', at: '2026-10-29T00:00:00Z' }], nextCursor: null };
    if (operation === 'lark.adapters') return { adapters: [] };
    throw new Error(operation);
  }) };
}
it('keeps live chat reachable and leaves first-run incomplete without a conversation send', async () => {
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} firstRunMode="unavailable" liveClient={backend()} baseline={DEMO_BASELINE} />);
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(await screen.findByText('verified host reply')).toBeInTheDocument();
  expect(screen.queryByText(/本地首次设置流程已完成/)).not.toBeInTheDocument();
});
it('does not expose simulated chat entries or completed setup from a development seed in live mode', async () => {
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={backend()} baseline={DEMO_BASELINE} firstRunMode="complete" chatSeed={{ sendMode: 'reply', replyText: 'simulated reply', availableTargetId: 'agent-squire' }} />);
  expect(screen.queryByRole('button', { name: /打开.*快捷对话/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/本地首次设置流程已完成/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(await screen.findByText('verified host reply')).toBeInTheDocument();
  expect(screen.queryByText('simulated reply')).not.toBeInTheDocument();
});
it('keeps explicit development preview composition on fixtures', async () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  try {
    render(<LinkedPreviewComposition />);
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: '首次设置' })).toBeInTheDocument();
  } finally { vi.unstubAllGlobals(); }
});
it('has no Agent 设置 entry rather than presenting fixture model rows in live mode', async () => {
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={backend()} />);
  fireEvent.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await waitFor(() => expect(screen.getByRole('menuitem', { name: '用户设置' })).toBeInTheDocument());
  expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
  expect(document.querySelector('.page-connections')).toBeNull();
  expect(screen.queryByRole('heading', { name: '凭据设置' })).toBeNull();
  expect(screen.queryByRole('button', { name: '添加 Key' })).toBeNull();
  expect(screen.queryByText('主模型服务')).not.toBeInTheDocument();
});
it.each(['linked-session', 'independent-seeds'])('isolates live runtime from upstream development %s', async (composition) => {
  const initial = createInitialSession();
  const session: LinkedSession = { ...initial,
    firstRun: { ...initial.firstRun, status: 'completed', step: 3 },
    modelServices: [{ id: 'mock-model', label: 'injected fixture model', state: 'connected', isDefault: true, method: 'account' }],
    agents: [{ id: 'mock-agent', name: 'injected fixture Agent', role: 'test', modelServiceId: 'mock-model', state: 'idle' }],
    org: { id: 'mock-org', name: 'injected fixture org', inviteCode: 'mock-invite' },
    members: [{ id: 'mock-member', identity: 'owner', name: 'injected fixture member', role: 'owner', state: 'joined' }],
    machines: [{ id: 'mock-machine', label: 'injected fixture computer', os: 'test', joinedAt: '2026-10-29', isThisComputer: true, status: 'online' }],
  };
  const onFirstRunAction = vi.fn();
  const agentGateway = { create: vi.fn(), update: vi.fn(), delete: vi.fn() };
  const stateSeed: StateSeed = composition === 'linked-session'
    ? {
        firstRun: selectFirstRunState(session),
        targets: selectChatTargets(session),
        connections: selectConnections(session),
        threads: selectChatSeed(session)?.threads,
      }
    : {};
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={backend()} baseline={DEMO_BASELINE} firstRunMode="complete"
    stateSeed={stateSeed}
    onFirstRunAction={onFirstRunAction}
    agentSeed={selectAgentSeed(session)} agentGateway={agentGateway}
    orgModel={selectOrgModel(session)} machineModel={selectMachineModel(session)} topologyModel={selectTopologyModel(session)} />);
  expect(screen.queryByText(/本地首次设置流程已完成/)).not.toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull());
  expect(screen.queryByText('injected fixture Agent')).not.toBeInTheDocument();
  expect(document.querySelector('[data-page="contacts"]')).toBeNull();
  expect(screen.queryByRole('button', { name: /创建 Agent/ })).toBeNull();
  expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  expect(screen.queryByText('injected fixture org')).not.toBeInTheDocument();
  expect(screen.queryByText('injected fixture computer')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '聊天' }));
  expect(await screen.findByText('verified host reply')).toBeInTheDocument();
  expect(onFirstRunAction).not.toHaveBeenCalled();
  expect(agentGateway.create).not.toHaveBeenCalled();
});

it('has no Agent 设置 entry and no platform-integration module when the real host has no adapter port', async () => {
  render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} liveClient={backend()} />);
  fireEvent.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await waitFor(() => expect(screen.getByRole('menuitem', { name: '用户设置' })).toBeInTheDocument());
  expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
  expect(document.querySelector('.page-connections')).toBeNull();
  expect(screen.queryByRole('heading', { name: '平台接入' })).toBeNull();
  expect(screen.queryByText('飞书已连接')).not.toBeInTheDocument();
});
