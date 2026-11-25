/**
 * Decision record (chat C1 ordinary page send + refresh recovery tests, 2026-11-05)
- The ordinary ConnectedChatPage keeps sending unavailable unless composition
  injects one frozen-contract send binding. These tests inject that binding, so
  they exercise the shared presentation and the adapter together; they are not
  real daemon, Contract Mock or browser evidence.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { GuiCapabilitiesProvider, createWebGuiCapabilities } from '../../src/app/capabilities';
import {
  CHAT_SEND_OPERATION_KEY_NAME,
  createChatSendApi,
  type ChatSendParams,
  type ChatSendStorage,
} from '../../src/features/chat-channels';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const TARGET = { uri: 'agent:owner:node:worker', displayName: 'worker' };
const CONVERSATION = 'conversation-1';

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map<string, string>(Object.entries(initial));
  const storage: ChatSendStorage = {
    getItem: (name) => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
  return { storage, values };
}

function frozenPort(send: (params: ChatSendParams) => Promise<unknown>, keyLookup?: () => Promise<unknown>) {
  const calls: Array<{ method: string; params: unknown }> = [];
  return {
    calls,
    port: {
      async send(params: ChatSendParams) { calls.push({ method: 'message.send', params }); return send(params); },
      async keyLookup(params: { operationKey: string }) {
        calls.push({ method: 'message.send.key_lookup', params });
        return keyLookup ? keyLookup() : { status: 'unknown', reason: 'not-found' };
      },
    },
  };
}

function acceptedSend(params: ChatSendParams) {
  return {
    ok: true, operationId: params.idempotencyKey, conversationId: params.conversationId, messageId: 'message-1',
    deliveries: [{ target: TARGET.uri, messageId: 'message-1', accepted: true }],
  };
}

function renderPage(binding?: Parameters<typeof ConnectedChatPage>[0]['chatSend']) {
  return render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage {...(binding ? { chatSend: binding } : {})} />
  </GuiCapabilitiesProvider>);
}

function typeAndSend(text = 'hello agent') {
  const input = screen.getByLabelText(`给 ${TARGET.displayName} 的消息`);
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}

it('renders no send surface when no send binding is injected', () => {
  renderPage();
  expect(screen.queryByText(/暂不可用/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(`给 ${TARGET.displayName} 的消息`)).not.toBeInTheDocument();
});

it('sends one canonical target through the injected adapter and reports only durable acceptance', async () => {
  const { storage, values } = memoryStorage();
  const { calls, port } = frozenPort(async (params) => acceptedSend(params));
  const api = createChatSendApi(port, { storage, randomBytes: () => new Uint8Array(32).fill(4) });
  renderPage({ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION });
  typeAndSend();
  expect(await screen.findByText('daemon 已持久接受该操作键；这不表示对方已收到。')).toBeInTheDocument();
  expect(calls).toHaveLength(1);
  const sent = calls[0].params as ChatSendParams;
  expect(sent.to).toEqual([TARGET.uri]);
  expect(sent.conversationId).toBe(CONVERSATION);
  expect(sent.message).toBe('hello agent');
  expect(sent.idempotencyKey).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(values.has(CHAT_SEND_OPERATION_KEY_NAME)).toBe(false);
  expect(screen.getByText(/不表示对方已收到/)).toBeInTheDocument();
  expect(screen.queryByText(/消息已送达/)).not.toBeInTheDocument();
});

it('shows the store failure and dispatches nothing when the key cannot be saved', async () => {
  const throwing: ChatSendStorage = {
    getItem: () => null,
    setItem() { throw new Error('quota'); },
    removeItem() { return; },
  };
  const { calls, port } = frozenPort(async (params) => acceptedSend(params));
  const api = createChatSendApi(port, { storage: throwing, randomBytes: () => new Uint8Array(32).fill(4) });
  renderPage({ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION });
  typeAndSend();
  expect(await screen.findByText('无法保存恢复键，发送未开始。')).toBeInTheDocument();
  expect(calls).toHaveLength(0);
});

it('recovers after a refresh with only the stored key and opens the recorded conversation', async () => {
  const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  const { calls, port } = frozenPort(
    async (params) => acceptedSend(params),
    async () => ({ status: 'accepted', commandId: 'k'.repeat(43), conversationId: 'conversation-9', messageId: 'message-9' }),
  );
  const api = createChatSendApi(port, { storage });
  const recovered: string[] = [];
  render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION,
      onConversationRecovered: (conversationId) => recovered.push(conversationId) }} />
  </GuiCapabilitiesProvider>);
  expect(await screen.findByText('该操作键已有持久发送记录；不表示对方收到，也不能证明原文与丢失输入完全一致。')).toBeInTheDocument();
  expect(calls).toEqual([{ method: 'message.send.key_lookup', params: { operationKey: 'k'.repeat(43) } }]);
  expect(calls.some(call => call.method === 'message.send')).toBe(false);
  expect(values.has(CHAT_SEND_OPERATION_KEY_NAME)).toBe(false);
  expect(recovered).toEqual(['conversation-9']);
});

it('keeps an unknown recovery key, offers manual lookup and never resends', async () => {
  const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  const { calls, port } = frozenPort(async (params) => acceptedSend(params));
  const api = createChatSendApi(port, { storage });
  renderPage({ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION });
  expect(await screen.findByText('无法确认该操作是否已持久接受；不会自动重发。')).toBeInTheDocument();
  expect(calls).toHaveLength(1);
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('k'.repeat(43));
  fireEvent.click(screen.getByRole('button', { name: '查询本操作键' }));
  await waitFor(() => expect(calls).toHaveLength(2));
  expect(calls.every(call => call.method === 'message.send.key_lookup')).toBe(true);
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('k'.repeat(43));
});

it('clears the pending key when the authenticated identity changes', async () => {
  const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  const { port } = frozenPort(async (params) => acceptedSend(params));
  const api = createChatSendApi(port, { storage });
  const view = render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION }} />
  </GuiCapabilitiesProvider>);
  expect(await screen.findByText('无法确认该操作是否已持久接受；不会自动重发。')).toBeInTheDocument();
  expect(values.size).toBe(1);
  view.rerender(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:other', target: TARGET, conversationId: CONVERSATION }} />
  </GuiCapabilitiesProvider>);
  await waitFor(() => expect(values.size).toBe(0));
});

it('ignores a prior identity lookup that resolves after identity change', async () => {
  let finish!: (value: unknown) => void;
  const { storage } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  const { port } = frozenPort(async (params) => acceptedSend(params), () => new Promise(resolve => { finish = resolve; }));
  const api = createChatSendApi(port, { storage });
  const recovered: string[] = [];
  const view = render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION,
      onConversationRecovered: id => recovered.push(id) }} />
  </GuiCapabilitiesProvider>);
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:other', target: TARGET, conversationId: CONVERSATION,
      onConversationRecovered: id => recovered.push(id) }} />
  </GuiCapabilitiesProvider>);
  finish({ status: 'accepted', commandId: 'k'.repeat(43), conversationId: 'old-conversation', messageId: 'old-message' });
  await waitFor(() => expect(api.pendingOperationKey()).toBeNull());
  expect(recovered).toEqual([]);
  expect(screen.queryByText(/该操作键已有持久发送记录/)).not.toBeInTheDocument();
});

it('ignores a prior identity send result after identity change', async () => {
  let finish!: (value: unknown) => void;
  const { storage } = memoryStorage();
  const { calls, port } = frozenPort(() => new Promise(resolve => { finish = resolve; }));
  const api = createChatSendApi(port, { storage, randomBytes: () => new Uint8Array(32).fill(4) });
  const view = render(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:owner', target: TARGET, conversationId: CONVERSATION }} />
  </GuiCapabilitiesProvider>);
  typeAndSend();
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<GuiCapabilitiesProvider value={createWebGuiCapabilities()}>
    <ConnectedChatPage chatSend={{ api, identity: 'user:other', target: TARGET, conversationId: CONVERSATION }} />
  </GuiCapabilitiesProvider>);
  const key = (calls[0].params as ChatSendParams).idempotencyKey;
  finish({ ok: true, operationId: key, conversationId: CONVERSATION,
    messageId: 'old-message', deliveries: [{ target: TARGET.uri, messageId: 'old-message' }] });
  await waitFor(() => expect(api.pendingOperationKey()).toBeNull());
  expect(screen.queryByText('daemon 已持久接受该操作键；这不表示对方已收到。')).not.toBeInTheDocument();
});

it('refuses a non-canonical agent target without dispatching', async () => {
  const { storage, values } = memoryStorage();
  const { calls, port } = frozenPort(async (params) => acceptedSend(params));
  const api = createChatSendApi(port, { storage, randomBytes: () => new Uint8Array(32).fill(4) });
  renderPage({ api, identity: 'user:owner', target: { uri: 'user:owner', displayName: 'owner' }, conversationId: CONVERSATION });
  const input = screen.getByLabelText('给 owner 的消息');
  fireEvent.change(input, { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  expect(await screen.findByText('只能向一个 canonical agent 目标发送；本次未派发。')).toBeInTheDocument();
  expect(calls).toHaveLength(0);
  expect(values.size).toBe(0);
});
