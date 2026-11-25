/**
 * Decision record (ordinary connected chat page, recovery visibility 2026-11-06)
- The owner reported the ordinary chat page permanently showing
  「无法确认该操作是否已持久接受；不会自动重发。」. The recovery run happened on
  page start, but the only key-lookup control lived on a selected target with an
  `unknown` operation, so a tab whose key was unsettled and no target selected had
  no way to query or abandon it. Every later send was then refused as
  `pending-operation`, which looks like a dead chat rather than one stalled send.
- This test pins the page contract: the recovery row is page level (no target
  selected), it offers exactly key lookup and explicit abandon, the copy never
  claims delivery or refusal, and abandoning does not dispatch anything.
- jsdom evidence only. The layout claim (`.page-chat--stacked`) is asserted as a
  class presence; pixel verification is separate browser evidence.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  GuiCapabilitiesProvider,
  createWebGuiCapabilities,
  type GuiCapabilities,
} from '../../src/app/capabilities';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';
import {
  CHAT_SEND_RECOVERY_UNKNOWN_NOTICE,
  CONNECTED_CHAT_RECOVERY_PENDING_NOTICE,
  createChatSendApi,
  type ChatSendParams,
  type ChatSendStorage,
  type MessageReadApi,
} from '../../src/features/chat-channels';

const targetUri = 'agent:owner:node:worker';
const key = 'k'.repeat(43);

function memory(initial: string | null) {
  let value = initial;
  const storage: ChatSendStorage = {
    getItem: () => value,
    setItem: (_name, next) => { value = next; },
    removeItem: () => { value = null; },
  };
  return { storage, read: () => value };
}

const read: MessageReadApi = {
  query: async () => [],
  history: async () => ({ entries: [], nextCursor: null }),
  lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  conversations: async () => ({ entries: [], nextCursor: null }),
};

function capabilities(send: ReturnType<typeof createChatSendApi>): GuiCapabilities {
  return {
    ...createWebGuiCapabilities(),
    messageRead: read,
    chatSend: send,
    chatTargets: async () => [{
      targetUri, targetKind: 'agent', deliverable: true, actor: targetUri,
    }],
    chatSession: { actor: 'user:owner', generation: 'generation-1' },
  };
}

it('keeps the unsettled key actionable on the page before any target is selected', async () => {
  const sent: ChatSendParams[] = [];
  const { storage, read: readKey } = memory(key);
  const send = createChatSendApi({
    send: async params => { sent.push(params); return {}; },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage });
  render(<GuiCapabilitiesProvider value={capabilities(send)}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  expect(await screen.findByText(CHAT_SEND_RECOVERY_UNKNOWN_NOTICE)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '查询原消息结果' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '放弃该操作键' })).toBeEnabled();
  expect(sent).toEqual([]);
  expect(readKey()).toBe(key);
  expect(screen.getByRole('region', { name: '聊天工作区' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: '聊天工作区' }).closest('.page-chat--stacked'))
    .not.toBeNull();
});

it('abandons the key only on the owner action, without dispatching or claiming failure', async () => {
  const sent: ChatSendParams[] = [];
  const { storage, read: readKey } = memory(key);
  const send = createChatSendApi({
    send: async params => { sent.push(params); return {}; },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage });
  render(<GuiCapabilitiesProvider value={capabilities(send)}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: '放弃该操作键' }));

  await waitFor(() => expect(readKey()).toBeNull());
  expect(sent).toEqual([]);
  expect(screen.getByText('已放弃本地恢复键；本次没有重发，也不代表那次发送失败。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查询原消息结果' })).not.toBeInTheDocument();
});

it('does not show a recovery row when the tab holds no key', async () => {
  const { storage } = memory(null);
  const send = createChatSendApi({
    send: async () => ({}),
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage });
  render(<GuiCapabilitiesProvider value={capabilities(send)}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  expect(await screen.findByRole('region', { name: '聊天工作区' })).toBeInTheDocument();
  expect(screen.queryByText(CHAT_SEND_RECOVERY_UNKNOWN_NOTICE)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '放弃该操作键' })).not.toBeInTheDocument();
});

it('states the unknown outcome once, above the conversation, and keeps it actionable', async () => {
  const { storage } = memory(null);
  const send = createChatSendApi({
    send: async () => { throw new Error('lost response'); },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage, randomBytes: () => new Uint8Array(32).fill(5) });
  render(<GuiCapabilitiesProvider value={{
    ...capabilities(send),
    messageRead: {
      ...read,
      query: async view => view === 'inbox'
        ? [{ messageId: 'm1', conversationId: 'c1', from: 'user:owner', to: targetUri, createdAtMs: 1 }]
        : [],
      history: async () => ({
        entries: [{ kind: 'message', messageId: 'm1', conversationId: 'c1', from: 'user:owner',
          to: targetUri, intent: 'request', createdAtMs: 1, message: 'hi', origin: {} }],
        nextCursor: null,
      }),
    },
  }}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
  const box = await screen.findByRole('textbox', { name: /的消息$/ });
  fireEvent.change(box, { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));

  await waitFor(() => expect(screen.getByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).toBeInTheDocument());
  expect(screen.getAllByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).toHaveLength(1);
  expect(screen.getByRole('button', { name: '查询原消息结果' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '放弃该操作键' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
});

it('offers one retry for a failed chat directory read instead of a dead page', async () => {
  let attempts = 0;
  const { storage } = memory(null);
  const send = createChatSendApi({
    send: async () => ({}),
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage });
  const base = capabilities(send);
  const failing: GuiCapabilities = {
    ...base,
    chatTargets: async () => {
      attempts += 1;
      if (attempts === 1) throw new Error('READ_FAILED');
      return [{ targetUri, targetKind: 'agent', deliverable: true, actor: targetUri }];
    },
  };
  render(<GuiCapabilitiesProvider value={failing}><ConnectedChatPage openedTargetIds={[targetUri]} /></GuiCapabilitiesProvider>);

  expect(await screen.findByText(/聊天对象读取失败/)).toBeInTheDocument();
  const retry = screen.getByRole('button', { name: '重试' });
  expect(screen.getAllByRole('button', { name: '重试' })).toHaveLength(1);
  fireEvent.click(retry);
  await waitFor(() => expect(attempts).toBe(2));
  expect(await screen.findByRole('button', { name: /worker/ })).toBeInTheDocument();
  expect(screen.queryByText(/聊天对象读取失败/)).not.toBeInTheDocument();
});

it('returns focus to the next useful control after the owner abandons the key', async () => {
  const { storage } = memory(key);
  const send = createChatSendApi({
    send: async () => ({}),
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage });
  render(<GuiCapabilitiesProvider value={capabilities(send)}><ConnectedChatPage openedTargetIds={[targetUri]} /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: '放弃该操作键' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '放弃该操作键' })).not.toBeInTheDocument());
  const target = await screen.findByRole('button', { name: /worker/ });
  await waitFor(() => expect(document.activeElement).toBe(target));
});

it('unblocks the composer after the owner abandons a selected-target unknown send', async () => {
  const sent: ChatSendParams[] = [];
  const { storage } = memory(null);
  const send = createChatSendApi({
    send: async params => { sent.push(params); throw new Error('lost response'); },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage, randomBytes: () => new Uint8Array(32).fill(5) });
  render(<GuiCapabilitiesProvider value={{
    ...capabilities(send),
    messageRead: {
      ...read,
      query: async view => view === 'inbox'
        ? [{ messageId: 'm1', conversationId: 'c1', from: 'user:owner', to: targetUri, createdAtMs: 1 }]
        : [],
      history: async () => ({
        entries: [{ kind: 'message', messageId: 'm1', conversationId: 'c1', from: 'user:owner',
          to: targetUri, intent: 'request', createdAtMs: 1, message: 'hi', origin: {} }],
        nextCursor: null,
      }),
    },
  }}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
  const box = await screen.findByRole('textbox', { name: /的消息$/ });
  fireEvent.change(box, { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));

  await waitFor(() => expect(screen.getByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).toBeInTheDocument());
  expect(sent).toHaveLength(1);
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();

  fireEvent.click(screen.getByRole('button', { name: '放弃该操作键' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled());
  expect(box).toHaveValue('hello');
  expect(sent).toHaveLength(1);
});

it('unblocks the target whose unknown send was abandoned from another target row', async () => {
  const otherUri = 'agent:owner:node:reviewer';
  const sent: ChatSendParams[] = [];
  const { storage, read: readKey } = memory(null);
  const send = createChatSendApi({
    send: async params => { sent.push(params); throw new Error('lost response'); },
    keyLookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  }, { storage, randomBytes: () => new Uint8Array(32).fill(5) });
  render(<GuiCapabilitiesProvider value={{
    ...capabilities(send),
    chatTargets: async () => [
      { targetUri, targetKind: 'agent', deliverable: true, actor: targetUri },
      { targetUri: otherUri, targetKind: 'agent', deliverable: true, actor: otherUri },
    ],
    messageRead: {
      ...read,
      query: async view => view === 'inbox'
        ? [{ messageId: 'm1', conversationId: 'c1', from: 'user:owner', to: targetUri, createdAtMs: 1 },
          { messageId: 'm2', conversationId: 'c2', from: 'user:owner', to: otherUri, createdAtMs: 2 }]
        : [],
      history: async () => ({ entries: [], nextCursor: null }),
    },
  }}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
  fireEvent.change(await screen.findByRole('textbox', { name: /worker 的消息$/ }), { target: { value: 'hello A' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));

  await waitFor(() => expect(screen.getByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).toBeInTheDocument());
  expect(sent).toHaveLength(1);
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();

  fireEvent.click(screen.getByRole('button', { name: /reviewer/ }));
  fireEvent.click(await screen.findByRole('button', { name: '放弃该操作键' }));
  await waitFor(() => expect(readKey()).toBeNull());
  expect(sent).toHaveLength(1);

  fireEvent.click(screen.getByRole('button', { name: /worker/ }));
  const box = await screen.findByRole('textbox', { name: /worker 的消息$/ });
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled());
  expect(box).toHaveValue('hello A');
  expect(screen.queryByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).not.toBeInTheDocument();
  expect(sent).toHaveLength(1);
});

it('keeps a late key lookup result on its owning target after the owner switches', async () => {
  const otherUri = 'agent:owner:node:reviewer';
  let finishLookup!: (value: unknown) => void;
  let markLookup!: () => void;
  const lookupStarted = new Promise<void>(resolve => { markLookup = resolve; });
  const sent: ChatSendParams[] = [];
  const { storage, read: readKey } = memory(null);
  const send = createChatSendApi({
    send: async params => { sent.push(params); throw new Error('lost response'); },
    keyLookup: () => new Promise(resolve => { finishLookup = resolve; markLookup(); }),
  }, { storage, randomBytes: () => new Uint8Array(32).fill(5) });
  render(<GuiCapabilitiesProvider value={{
    ...capabilities(send),
    chatTargets: async () => [
      { targetUri, targetKind: 'agent', deliverable: true, actor: targetUri },
      { targetUri: otherUri, targetKind: 'agent', deliverable: true, actor: otherUri },
    ],
    messageRead: {
      ...read,
      query: async view => view === 'inbox'
        ? [{ messageId: 'm1', conversationId: 'c1', from: 'user:owner', to: targetUri, createdAtMs: 1 },
          { messageId: 'm2', conversationId: 'c2', from: 'user:owner', to: otherUri, createdAtMs: 2 }]
        : [],
      history: async () => ({ entries: [], nextCursor: null }),
    },
  }}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  fireEvent.click(await screen.findByRole('button', { name: /worker/ }));
  fireEvent.change(await screen.findByRole('textbox', { name: /worker 的消息$/ }), { target: { value: 'hello A' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(screen.getByText('发送结果未知；不会自动重发，可用该操作键手动查询。')).toBeInTheDocument());
  expect(sent).toHaveLength(1);

  fireEvent.click(screen.getByRole('button', { name: '查询原消息结果' }));
  await lookupStarted;
  fireEvent.click(screen.getByRole('button', { name: /reviewer/ }));
  await screen.findByRole('textbox', { name: /reviewer 的消息$/ });
  finishLookup({ status: 'unknown', reason: 'not-found' });

  expect(await screen.findByText(CHAT_SEND_RECOVERY_UNKNOWN_NOTICE)).toBeInTheDocument();
  expect(screen.queryByText(CONNECTED_CHAT_RECOVERY_PENDING_NOTICE)).not.toBeInTheDocument();
  expect(readKey()).not.toBeNull();
  expect(sent).toHaveLength(1);

  fireEvent.click(screen.getByRole('button', { name: '放弃该操作键' }));
  await waitFor(() => expect(readKey()).toBeNull());
  fireEvent.click(screen.getByRole('button', { name: /worker/ }));
  const box = await screen.findByRole('textbox', { name: /worker 的消息$/ });
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled());
  expect(box).toHaveValue('hello A');
  expect(sent).toHaveLength(1);
});
