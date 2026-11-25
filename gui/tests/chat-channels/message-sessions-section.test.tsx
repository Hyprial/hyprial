import { act, fireEvent, render, screen } from '@testing-library/react';
import { MethodRefusedError } from '../../src/client';
import {
  type MessageReadApi,
  MessageSessionsSection,
} from '../../src/features/chat-channels';
import { GuiCapabilitiesProvider, createWebGuiCapabilities } from '../../src/app/capabilities';
import { LiveChatPage, type ChatPageRuntime } from '../../src/pages/live/LiveChatPage';
import type { AgentDirectoryRow, AgentsApi } from '../../src/features/agents/impl/api';

const row = { messageId: 'msg-1', conversationId: 'conv-1', sender: 'user:owner',
  recipient: 'agent:owner:node:worker', intent: 'request', createdAtMs: 1000 };
const entry = { kind: 'message' as const, messageId: 'msg-1', conversationId: 'conv-1',
  from: 'user:owner', to: 'agent:owner:node:worker', intent: 'request' as const, createdAtMs: 1000,
  message: 'hello', origin: {} };
const api = (overrides: Partial<MessageReadApi> = {}): MessageReadApi => ({
  query: async () => [row], conversations: async () => ({ entries: [{ documentId: 'c.conv-1', conversationId: 'conv-1', peer: 'agent:owner:node:worker', lastMessageAtMs: 1000 }], nextCursor: null }),
  history: async () => ({ entries: [entry], nextCursor: null }),
  lookup: async () => ({ status: 'unknown', reason: 'not-found' }), ...overrides,
});

const agent = (uri: string, displayName = uri): AgentDirectoryRow => ({
  actor: uri.split(':').at(-1)!, uri, displayName, owner: '', host: '', provider: null,
  model: null, harness: null, preferredHarness: null, capabilities: [], runState: { runtime: 'online' },
  cwd: null, homeMtimeMs: null, createdAtMs: null, hasLastSessionId: null, lastActiveAtMs: null,
});

const agentsFor = (peers: readonly string[]): AgentsApi => ({ list: async () => peers.map(peer => agent(peer)) }) as unknown as AgentsApi;

function renderSessions(ui: React.ReactElement, peers: readonly string[] = ['agent:owner:node:worker']) {
  return render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsFor(peers) }}>{ui}</GuiCapabilitiesProvider>);
}

it('loads durable conversation rows and opens their participant-scoped history', async () => {
  renderSessions(<MessageSessionsSection api={api()} />);
  const button = await screen.findByRole('button', { name: 'agent:owner:node:worker' });
  fireEvent.click(button);
  expect(await screen.findByText(/hello/)).toBeInTheDocument();
});

it('labels agent history authors from the URI-matched directory row', async () => {
  const uri = 'agent:owner:node:a.worker';
  const inbound = { ...entry, from: uri, to: 'user:owner' };
  render(<GuiCapabilitiesProvider value={{
    ...createWebGuiCapabilities(),
    agents: { list: async () => [agent(uri, 'Alfred')] } as unknown as AgentsApi,
  }}>
    <MessageSessionsSection api={api({
      conversations: async () => ({ entries: [{ documentId: 'c.conv-1', conversationId: 'conv-1', peer: uri, lastMessageAtMs: 1000 }], nextCursor: null }),
      history: async () => ({ entries: [inbound], nextCursor: null }),
    })} />
  </GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: 'Alfred' }));
  expect(await screen.findByText(/hello/)).toBeInTheDocument();
  expect(screen.getByText('Alfred', { selector: '.chat-message strong' })).toBeInTheDocument();
  expect(screen.queryByText(uri, { selector: '.chat-message strong' })).not.toBeInTheDocument();
});

it('shows only directory states without fabricating agents', async () => {
  const absent = render(<MessageSessionsSection />);
  expect(screen.getByText('历史会话目录尚未确认')).toBeInTheDocument();
  expect(screen.queryByText('Agent 目录暂不可用')).not.toBeInTheDocument();
  expect(screen.queryByText('最近消息中的会话')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /agent:owner:node:worker/ })).not.toBeInTheDocument();
  absent.unmount();
  const empty = renderSessions(<MessageSessionsSection api={api({ conversations: async () => ({ entries: [], nextCursor: null }) })} />);
  expect(await screen.findByText('暂无历史会话')).toBeInTheDocument();
  empty.unmount();
  renderSessions(<MessageSessionsSection api={api({ conversations: async () => { throw new Error('offline'); } })} />);
  expect(await screen.findByText('历史会话目录读取失败')).toBeInTheDocument();
});

it('renders no directory block when the daemon has no conversation directory method', async () => {
  renderSessions(<MessageSessionsSection api={api({ conversations: async () => { throw new MethodRefusedError('conversations.list', 'GUI_CAPABILITY_UNAVAILABLE'); } })} />);
  expect(await screen.findByRole('button', { name: 'agent:owner:node:worker' })).toBeInTheDocument();
  expect(screen.queryByText(/历史会话目录(暂不可用|读取失败)/)).not.toBeInTheDocument();
  expect(screen.queryByText('暂无历史会话')).not.toBeInTheDocument();
});

it('shows history failure without inventing a message', async () => {
  renderSessions(<MessageSessionsSection api={api({ history: async () => { throw new Error('offline'); } })} />);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:worker' }));
  expect(await screen.findByText('消息暂时无法读取。请稍后再试。')).toBeInTheDocument();
  expect(screen.queryByText('hello')).not.toBeInTheDocument();
});

it('uses the host-injected read capability on the live chat page', async () => {
  const snapshot = { status: 'ready', targets: [], activeTargetId: null, drafts: {}, entries: {} };
  const live = { status: 'ready', chat: { subscribe: () => () => {}, getSnapshot: () => snapshot } } as unknown as ChatPageRuntime;
  render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead: api(), agents: agentsFor(['agent:owner:node:worker']) }}>
    <LiveChatPage live={live} />
  </GuiCapabilitiesProvider>);
  expect(await screen.findByRole('button', { name: 'agent:owner:node:worker' })).toBeInTheDocument();
});

it('passes the history cursor and keeps earlier messages when loading more', async () => {
  const cursors: Array<string | undefined> = [];
  const second = { ...entry, messageId: 'msg-2', message: 'second', createdAtMs: 2000 };
  renderSessions(<MessageSessionsSection api={api({ history: async (_conversation, cursor) => {
    cursors.push(cursor);
    return cursor ? { entries: [second], nextCursor: null } : { entries: [entry], nextCursor: 'next-1' };
  } })} />);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:worker' }));
  fireEvent.click(await screen.findByRole('button', { name: '加载更多' }));
  expect(await screen.findByText(/second/)).toBeInTheDocument();
  expect(screen.getByText(/hello/)).toBeInTheDocument();
  expect(cursors).toEqual([undefined, 'next-1']);
});

it('matches a settled conversation only through the exact agent actor', async () => {
  const inbound = { messageId: 'in-1', conversationId: 'conv-in', from: 'agent:owner:node:inbound',
    to: 'user:owner', kind: 'message' as const, intent: 'request' as const, createdAtMs: 3000, message: 'ping', origin: {} };
  renderSessions(<MessageSessionsSection api={api({
    query: async () => [],
    conversations: async () => ({ entries: [{ documentId: 'c.conv-in', conversationId: 'conv-in', peer: inbound.from, lastMessageAtMs: 3000 }], nextCursor: null }),
    history: async () => ({ entries: [inbound], nextCursor: null }),
  })} />, ['agent:owner:node:inbound']);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:inbound' }));
  expect(await screen.findByText(/ping/)).toBeInTheDocument();
});

it('loads older conversations with the directory cursor without listing peers as agents', async () => {
  const cursors: Array<string | undefined> = [];
  renderSessions(<MessageSessionsSection api={api({ conversations: async cursor => {
    cursors.push(cursor);
    return cursor
      ? { entries: [{ documentId: 'c.conv-old', conversationId: 'conv-old', peer: 'agent:owner:node:old', lastMessageAtMs: 500 }], nextCursor: null }
      : { entries: [{ documentId: 'c.conv-1', conversationId: 'conv-1', peer: 'agent:owner:node:worker', lastMessageAtMs: 1000 }], nextCursor: 'next-page' };
  } })} />);
  fireEvent.click(await screen.findByRole('button', { name: '加载更多会话' }));
  expect(cursors).toEqual([undefined, 'next-page']);
  expect(screen.queryByRole('button', { name: 'agent:owner:node:old' })).not.toBeInTheDocument();
});

it.each(['resolve', 'reject'] as const)('ignores stale directory pagination after capability switch (%s)', async outcome => {
  let finish!: (page: Awaited<ReturnType<MessageReadApi['conversations']>>) => void;
  let fail!: (error: Error) => void;
  const oldApi = api({ conversations: cursor => cursor
    ? new Promise((resolve, reject) => { finish = resolve; fail = reject; })
    : Promise.resolve({ entries: [{ documentId: 'c.old', conversationId: 'old', peer: 'old-peer', lastMessageAtMs: 1 }], nextCursor: 'more' }) });
  const newApi = api({ conversations: async () => ({ entries: [{ documentId: 'c.new', conversationId: 'new', peer: 'new-peer', lastMessageAtMs: 2 }], nextCursor: null }) });
  const view = renderSessions(<MessageSessionsSection api={oldApi} />);
  fireEvent.click(await screen.findByRole('button', { name: '加载更多会话' }));
  view.rerender(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsFor(['agent:owner:node:worker']) }}><MessageSessionsSection api={newApi} /></GuiCapabilitiesProvider>);
  expect(await screen.findByRole('button', { name: 'agent:owner:node:worker' })).toBeInTheDocument();
  await act(async () => {
    if (outcome === 'resolve') finish({ entries: [{ documentId: 'c.late', conversationId: 'late', peer: 'late-peer', lastMessageAtMs: 0 }], nextCursor: null });
    else fail(new Error('stale failure'));
  });
  expect(screen.getByRole('button', { name: 'agent:owner:node:worker' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'late-peer' })).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it.each(['resolve', 'reject'] as const)('ignores stale history pagination after an API switch (%s)', async outcome => {
  let resolveOld!: (page: Awaited<ReturnType<MessageReadApi['history']>>) => void;
  let rejectOld!: (error: Error) => void;
  const oldApi = api({ history: (_conversation, cursor) => cursor
    ? new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; })
    : Promise.resolve({ entries: [entry], nextCursor: 'more' }) });
  const newEntry = { ...entry, messageId: 'new-message', message: 'new history' };
  const newApi = api({ history: async () => ({ entries: [newEntry], nextCursor: null }) });
  const view = renderSessions(<MessageSessionsSection api={oldApi} />);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:worker' }));
  fireEvent.click(await screen.findByRole('button', { name: '加载更多' }));
  view.rerender(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsFor(['agent:owner:node:worker']) }}><MessageSessionsSection api={newApi} /></GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:worker' }));
  expect(await screen.findByText(/new history/)).toBeInTheDocument();
  await act(async () => outcome === 'resolve'
    ? resolveOld({ entries: [{ ...entry, messageId: 'late', message: 'late history' }], nextCursor: null })
    : rejectOld(new Error('stale')));
  expect(screen.getByText(/new history/)).toBeInTheDocument();
  expect(screen.queryByText(/late history/)).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('hides history immediately when the read capability is removed', async () => {
  const view = renderSessions(<MessageSessionsSection api={api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'agent:owner:node:worker' }));
  expect(await screen.findByText(/hello/)).toBeInTheDocument();
  view.rerender(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), agents: agentsFor(['agent:owner:node:worker']) }}><MessageSessionsSection /></GuiCapabilitiesProvider>);
  expect(screen.queryByText(/hello/)).not.toBeInTheDocument();
});
