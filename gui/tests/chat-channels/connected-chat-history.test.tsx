/**
 * Decision record (ordinary connected chat history, layout increment 2026-11-05)
- The ordinary connected page now renders the shared AgentChatSection. Its
  history is requested only for a conversation that `message.query` confirmed
  for a directory agent, and the reader is fenced so a late response for an old
  target cannot overwrite the selected one.
- These tests keep the previous behavioural coverage (read through the shared
  read API, ignore late responses, report a failed read without inventing
  messages) on the new presentation; pagination stays covered by
  MessageSessionsSection, which the legacy live page still renders.
- Slice G1 (2026-11-18): history is read from the conversation document, so a
  discovered conversation is readable once `conversations.list` names its
  document (`heads`); history calls carry the document id `c.<send id>`.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import type { MessageReadApi } from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const TARGET = 'agent:owner:node:one';
const row = (id: string, at: number) => ({ messageId: `directory-${id}`, conversationId: id,
  sender: 'user:owner', recipient: `agent:owner:node:${id}`, createdAtMs: at });
const entry = (id: string, message: string) => ({ kind: 'message' as const, messageId: message,
  conversationId: id, from: 'user:owner', to: `agent:owner:node:${id}`, intent: 'request' as const,
  createdAtMs: 1000, message, origin: {} });

const heads = (...ids: string[]) => async () => ({ entries: ids.map((id, index) => ({
  documentId: `c.${id}`, conversationId: id, peer: `agent:owner:node:${id}`, lastMessageAtMs: 2000 - index,
})), nextCursor: null });

function capabilities(read: MessageReadApi, targets = [TARGET]): GuiCapabilities {
  return {
    ...createWebGuiCapabilities(),
    messageRead: read,
    chatTargets: async () => targets.map(targetUri => ({
      targetKind: 'agent', targetUri, deliverable: true, actor: targetUri, status: 'online',
    })),
    chatSession: { actor: 'user:owner', generation: 'one' },
  };
}

it('adds a directory Agent to chat only after explicit opening and retains opened entries on return', async () => {
  const read: MessageReadApi = {
    query: async () => [], conversations: async () => ({ entries: [], nextCursor: null }),
    history: vi.fn(async () => ({ entries: [], nextCursor: null })),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  const caps = capabilities(read, [TARGET, 'agent:owner:node:unopened']);
  const view = render(<GuiCapabilitiesProvider value={caps}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  await screen.findByText('暂无聊天对象');
  expect(screen.queryByRole('button', { name: /one|unopened/ })).toBeNull();
  view.rerender(<GuiCapabilitiesProvider value={caps}><ConnectedChatPage initialTargetId={TARGET} /></GuiCapabilitiesProvider>);
  expect(await screen.findByRole('button', { name: /one/ })).toBeVisible();
  expect(screen.queryByRole('button', { name: /unopened/ })).toBeNull();
  view.unmount();
  render(<GuiCapabilitiesProvider value={caps}><ConnectedChatPage openedTargetIds={[TARGET]} /></GuiCapabilitiesProvider>);
  expect(await screen.findByRole('button', { name: /one/ })).toBeVisible();
  expect(read.history).not.toHaveBeenCalled();
});

it('reads the conversation discovered for the selected agent and keeps it across a refresh', async () => {
  const calls: Array<[string, string | undefined]> = [];
  const api: MessageReadApi = {
    query: async view => view === 'inbox' ? [row('one', 2000)] : [],
    history: async (id, cursor) => {
      calls.push([id, cursor]);
      return { entries: [entry(id, 'newer')], nextCursor: null };
    },
    conversations: heads('one'),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={capabilities(api)}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /one/ }));
  expect(await screen.findByText(/newer/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
  await waitFor(() => expect(calls).toEqual([['c.one', undefined], ['c.one', undefined]]));
  expect(screen.getByText(/newer/)).toBeInTheDocument();
});

it('keeps refresh in the conversation heading without an empty action strip', async () => {
  const api: MessageReadApi = {
    query: async view => view === 'inbox' ? [row('one', 2000)] : [],
    history: async id => ({ entries: [entry(id, 'newer')], nextCursor: null }),
    conversations: heads('one'),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={capabilities(api)}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /one/ }));
  expect(await screen.findByText(/newer/)).toBeInTheDocument();
  const heading = document.querySelector('.chat-workspace__heading');
  expect(heading).not.toBeNull();
  expect(within(heading as HTMLElement).getByRole('button', { name: '刷新消息' })).toBeVisible();
  expect(within(heading as HTMLElement).getByRole('button', { name: '信息' })).toBeVisible();
  expect(document.querySelector('.chat-workspace__conversation-toolbar')).toBeNull();
  expect(document.querySelector('.page-chat > .chat-page-toolbar')).toBeNull();
});

it('shows a reply without refresh and retains the message already displayed', async () => {
  let replied = false;
  const history = vi.fn(async (id: string) => ({
    entries: replied ? [entry(id, '你好'), { ...entry(id, '测试成功'), from: TARGET }] : [entry(id, '你好')], nextCursor: null,
  }));
  const api: MessageReadApi = {
    query: async () => [row('one', 2000)], history,
    conversations: heads('one'),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={capabilities(api)}><ConnectedChatPage initialTargetId={TARGET} /></GuiCapabilitiesProvider>);
  expect(await screen.findByText('你好')).toBeVisible();
  replied = true;
  expect(await screen.findByText('测试成功', {}, { timeout: 5000 })).toBeVisible();
  expect(screen.getAllByText('你好')).toHaveLength(1);
});

it('ignores a late history response after selecting another target', async () => {
  let resolveFirst!: (page: Awaited<ReturnType<MessageReadApi['history']>>) => void;
  const api: MessageReadApi = {
    query: async view => view === 'inbox' ? [row('one', 2000), row('two', 1000)] : [],
    history: (id) => id === 'c.one'
      ? new Promise(resolve => { resolveFirst = resolve; })
      : Promise.resolve({ entries: [entry(id, 'second')], nextCursor: null }),
    conversations: heads('one', 'two'),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={capabilities(api, [TARGET, 'agent:owner:node:two'])}>
    <ConnectedChatPage />
  </GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /one/ }));
  fireEvent.click(screen.getByRole('button', { name: /two/ }));
  expect(await screen.findByText(/second/)).toBeInTheDocument();
  await act(async () => resolveFirst({ entries: [entry('one', 'late')], nextCursor: null }));
  expect(screen.queryByText(/late/)).not.toBeInTheDocument();
  expect(screen.getByText(/second/)).toBeInTheDocument();
});

it('reports a failed history read without inventing messages and retries on refresh', async () => {
  let attempts = 0;
  const api: MessageReadApi = {
    query: async view => view === 'inbox' ? [row('one', 2000)] : [],
    history: async (id) => {
      attempts += 1;
      if (attempts === 1) throw new Error('temporary read failure');
      return { entries: [entry(id, 'recovered')], nextCursor: null };
    },
    conversations: heads('one'),
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={capabilities(api)}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /one/ }));
  expect(await screen.findByText('历史消息加载失败。')).toBeInTheDocument();
  expect(screen.queryByText(/recovered/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '刷新消息' }));
  expect(await screen.findByText(/recovered/)).toBeInTheDocument();
  expect(attempts).toBe(2);
});

it('offers a first message when the agent has no conversation yet', async () => {
  const calls: string[] = [];
  const api: MessageReadApi = {
    query: async view => { calls.push(view); return []; },
    history: async (id) => { calls.push(`history:${id}`); return { entries: [], nextCursor: null }; },
    conversations: async () => { throw new Error('unexpected directory'); },
    lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  };
  render(<GuiCapabilitiesProvider value={{ ...capabilities(api), chatSend: {
    storageName: 'test', pendingOperationKey: () => null,
    send: async () => { throw new Error('must not dispatch'); },
    recover: async () => ({ status: 'idle', notice: '没有可恢复的发送操作键。' }),
    sendWithKey: async () => { throw new Error('must not dispatch'); },
    recoverKey: async () => ({ status: 'idle', notice: '没有可恢复的发送操作键。' }),
    clear: () => undefined,
  } }}><ConnectedChatPage initialTargetId={TARGET} /></GuiCapabilitiesProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /one/ }));
  expect(await screen.findByText('还没有会话；发送第一条消息后会开始新会话。')).toBeInTheDocument();
  expect(screen.getByText('发送第一条消息后会开始新会话。')).toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } });
  expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
  expect(calls).toEqual(['inbox', 'outbox']);
});
