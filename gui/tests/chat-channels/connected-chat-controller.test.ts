/**
 * Decision record (connected chat controller, layout increment 2026-11-05)
- These tests pin the method projection that the ordinary chat layout consumes:
  `targets` is the only directory authority, `message.query` only discovers an
  already-existing conversation, `message.history` is requested only for that
  conversation, and the frozen `message.send` adapter keeps the #2441 dispatch and
  recovery invariants.
- They are jsdom/API evidence. They do not replace a browser journey against the
  Contract Mock or an isolated daemon.
- Slice G1 (2026-11-18): history is the conversation document. A conversation
  is readable only through the `c.` id its `conversations.list` head names, so
  tests that read history list a head (`listing`) and expect the document id
  (`documentOf`) in history calls; the send id stays the one `message.send`
  takes.
- PR 2936 review (2026-11-18): an accepted send no longer marks a chat whose
  document was never read as `ready`; it stays `unread` until the document is
  attached, so the two send tests here expect `unread` after acceptance.
 */

import { DomainError, HttpError } from '../../src/client';
import {
  createChatSendApi,
  type ChatSendParams,
  type ChatSendStorage,
  type HistoryPage,
} from '../../src/features/chat-channels';
import {
  ConnectedChatController,
} from '../../src/features/chat-channels';

const TARGET = 'agent:owner:node:worker';
const OTHER = 'agent:owner:node:other';
const CONVERSATION = 'conversation-1';
const documentOf = (conversationId: string) => `c.${conversationId}`;
const DOCUMENT = documentOf(CONVERSATION);
const ENTRY = {
  kind: 'message' as const, messageId: 'message-1', conversationId: CONVERSATION,
  from: 'user:owner', to: TARGET, intent: 'request' as const, createdAtMs: 1000,
  message: 'hi', origin: {},
};

function row(targetUri: string, overrides: Record<string, unknown> = {}) {
  return { targetKind: 'agent', targetUri, deliverable: true, actor: targetUri, status: 'online', ...overrides };
}

/** A `conversations.list` port that names a document for each send id and peer. */
function listing(...rows: ReadonlyArray<readonly [conversationId: string, peer: string, at?: number]>) {
  return async () => ({
    entries: rows.map(([conversationId, peer, at = 1]) => ({
      documentId: documentOf(conversationId), conversationId, peer, lastMessageAtMs: at,
    })),
    nextCursor: null,
  });
}

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map<string, string>(Object.entries(initial));
  const storage: ChatSendStorage = {
    getItem: name => values.get(name) ?? null,
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
  return { storage, values };
}

it('uses incremental cursors without losing a partial page or repeating accepted messages', async () => {
  const history = vi.fn(async (_id: string, cursor?: string, mode?: string) => {
    if (mode === 'tail') return { entries: [ENTRY], nextCursor: null, headCursor: 'head-1', nextOlderCursor: 'older-1' };
    if (cursor === 'head-1') return { entries: [ENTRY, { ...ENTRY, messageId: 'reply-1', createdAtMs: 2000 }], nextCursor: 'more-after', headCursor: 'newest' };
    return { entries: [{ ...ENTRY, messageId: 'reply-2', createdAtMs: 3000 }], nextCursor: null, headCursor: 'head-3' };
  });
  const controller = new ConnectedChatController({ targets: async () => [row(TARGET)],
    query: async () => [ENTRY], conversations: listing([CONVERSATION, TARGET]), history });
  await controller.start(); controller.select(TARGET); await Promise.resolve();
  controller.setDraft(TARGET, '尚未发送');
  await controller.refresh();
  await controller.refresh();
  expect(history.mock.calls).toEqual([[DOCUMENT, undefined, 'tail'], [DOCUMENT, 'head-1', 'after'], [DOCUMENT, 'more-after', 'after']]);
  expect(controller.getSnapshot().entries[TARGET].messages.map(message => message.id)).toEqual(['message-1', 'reply-1', 'reply-2']);
  expect(controller.getSnapshot().entries[TARGET].nextCursor).toBe('older-1');
  expect(controller.getSnapshot().drafts[TARGET]).toBe('尚未发送');
});

it('coalesces refresh reads and rejects a late read after selecting another target', async () => {
  let finish!: (page: HistoryPage) => void;
  const history = vi.fn(async (_id: string, _cursor?: string, mode?: string): Promise<HistoryPage> => {
    if (mode === 'after') return new Promise(resolve => { finish = resolve; });
    return { entries: [ENTRY], nextCursor: null, headCursor: 'head' };
  });
  const controller = new ConnectedChatController({ targets: async () => [row(TARGET), row(OTHER)],
    query: async () => [ENTRY], conversations: listing([CONVERSATION, TARGET]), history });
  await controller.start(); controller.select(TARGET); await Promise.resolve();
  const pending = controller.refresh(); await controller.refresh();
  expect(history).toHaveBeenCalledTimes(2);
  controller.select(OTHER);
  finish({ entries: [{ ...ENTRY, messageId: 'late' }], nextCursor: null });
  await pending;
  expect(controller.getSnapshot().entries[TARGET].messages.map(message => message.id)).toEqual(['message-1']);
  expect(controller.getSnapshot().entries[OTHER].messages).toEqual([]);
});

it('selects the newest discovered conversation and can read subsequent history pages', async () => {
  const history = vi.fn(async (_id: string, cursor?: string) => cursor
    ? { entries: [{ ...ENTRY, messageId: 'second', message: 'older or later page' }], nextCursor: null }
    : { entries: [ENTRY], nextCursor: 'page-2' });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async view => view === 'inbox' ? [
      { messageId: 'old', conversationId: 'old-conversation', from: 'user:owner', to: TARGET, createdAtMs: 1 },
      { messageId: 'new', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 20 },
    ] : [],
    conversations: async () => ({ entries: [
      { documentId: DOCUMENT, conversationId: CONVERSATION, peer: TARGET, lastMessageAtMs: 20 },
      { documentId: documentOf('old-conversation'), conversationId: 'old-conversation', peer: TARGET, lastMessageAtMs: 1 },
    ], nextCursor: null }),
    history, identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  await Promise.resolve();
  expect(history.mock.calls[0][0]).toBe(DOCUMENT);
  expect(controller.getSnapshot().entries[TARGET].nextCursor).toBe('page-2');
  await controller.loadMore(TARGET);
  expect(history).toHaveBeenLastCalledWith(DOCUMENT, 'page-2', 'older');
  expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.id)).toEqual(['second', 'message-1']);
  expect(controller.getSnapshot().entries[TARGET].nextCursor).toBeNull();
  await controller.loadMore(TARGET);
  expect(history).toHaveBeenCalledTimes(2);
});

it('restores conversations from the paged daemon directory even when message.query is empty', async () => {
  const history = vi.fn(async () => ({ entries: [ENTRY], nextCursor: null }));
  const conversations = vi.fn(async (cursor?: string) => cursor
    ? { entries: [{ documentId: documentOf('old'), conversationId: 'old', peer: TARGET, lastMessageAtMs: 1 }], nextCursor: null }
    : { entries: [{ documentId: DOCUMENT, conversationId: CONVERSATION, peer: TARGET, lastMessageAtMs: 10 }], nextCursor: 'older' });
  const controller = new ConnectedChatController({ targets: async () => [row(TARGET)],
    query: async () => [], conversations, history });
  await controller.start();
  controller.select(TARGET);
  await Promise.resolve();
  expect(history).toHaveBeenLastCalledWith(DOCUMENT, undefined, 'tail');
  expect(conversations.mock.calls).toEqual([[undefined], ['older']]);
  controller.selectConversation(TARGET, 'old');
  await Promise.resolve();
  expect(history).toHaveBeenLastCalledWith(documentOf('old'), undefined, 'tail');
  controller.selectConversation(TARGET, 'not-in-directory');
  expect(history).toHaveBeenCalledTimes(2);
});

it('does not duplicate page reads or apply a late page after refresh', async () => {
  let finish!: (page: HistoryPage) => void;
  const history = vi.fn((_id: string, cursor?: string) => cursor
    ? new Promise<HistoryPage>(resolve => { finish = resolve; })
    : Promise.resolve({ entries: [ENTRY], nextCursor: 'page-2' }));
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'a', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    conversations: listing([CONVERSATION, TARGET]),
    history,
  });
  await controller.start();
  controller.select(TARGET);
  await Promise.resolve();
  const pending = controller.loadMore(TARGET);
  await controller.loadMore(TARGET);
  expect(history).toHaveBeenCalledTimes(2);
  controller.select(TARGET);
  await Promise.resolve();
  finish({ entries: [{ ...ENTRY, messageId: 'late' }], nextCursor: null });
  await pending;
  expect(controller.getSnapshot().entries[TARGET].messages.map(row => row.id)).toEqual(['message-1']);
  expect(controller.getSnapshot().entries[TARGET].nextCursor).toBe('page-2');
});

it('retains loaded messages and the cursor when a subsequent read fails', async () => {
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'a', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    conversations: listing([CONVERSATION, TARGET]),
    history: async (_id, cursor) => {
      if (cursor) throw new Error('offline');
      return { entries: [ENTRY], nextCursor: 'page-2' };
    },
  });
  await controller.start();
  controller.select(TARGET);
  await Promise.resolve();
  await controller.loadMore(TARGET);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({ historyStatus: 'ready',
    historyError: '历史消息读取失败：offline', loadingMore: false, nextCursor: 'page-2' });
  expect(controller.getSnapshot().entries[TARGET].messages).toHaveLength(1);
});

it('projects the authoritative directory and only the conversations it confirms', async () => {
  const historyCalls: string[] = [];
  const controller = new ConnectedChatController({
    targets: async () => [
      row(TARGET),
      row(OTHER, { deliverable: false }),
      { ...row('user:someone'), targetKind: 'user' },
      row('not-a-canonical-uri'),
    ],
    query: async view => view === 'inbox'
      ? [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }]
      : [],
    history: async id => { historyCalls.push(id); return { entries: [ENTRY], nextCursor: null }; },
    identity: 'user:owner',
  });
  await controller.start();
  const snapshot = controller.getSnapshot();

  expect(snapshot.status).toBe('ready');
  expect(snapshot.targets.map(target => target.id)).toEqual([TARGET]);
  expect(snapshot.activeTargetId).toBeNull();
  expect(snapshot.entries).toEqual({});
  expect(historyCalls).toEqual([]);
});

it('reads history only after selecting the target with a confirmed conversation', async () => {
  const historyCalls: string[] = [];
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async view => view === 'inbox'
      ? [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }]
      : [],
    conversations: listing([CONVERSATION, TARGET]),
    history: async id => { historyCalls.push(id); return { entries: [ENTRY], nextCursor: null }; },
    identity: 'user:owner',
  });
  await controller.start();

  controller.select(OTHER);
  expect(controller.getSnapshot().entries[OTHER]).toMatchObject({
    conversationId: null, historyStatus: 'unconfirmed', messages: [],
  });
  expect(historyCalls).toEqual([]);

  controller.select(TARGET);
  await Promise.resolve();
  await Promise.resolve();
  expect(historyCalls).toEqual([DOCUMENT]);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    conversationId: CONVERSATION, historyStatus: 'ready',
    messages: [{ id: 'message-1', author: 'user', text: 'hi' }],
  });
});

it('drops a late history response for the previously selected target', async () => {
  let finishFirst!: (page: HistoryPage) => void;
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async view => view === 'inbox'
      ? [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 2 },
        { messageId: 'message-2', conversationId: 'conversation-2', from: 'user:owner', to: OTHER, createdAtMs: 1 }]
      : [],
    conversations: listing([CONVERSATION, TARGET], ['conversation-2', OTHER]),
    history: (id) => id === DOCUMENT
      ? new Promise(resolve => { finishFirst = resolve; })
      : Promise.resolve({ entries: [{ ...ENTRY, messageId: 'message-3', conversationId: 'conversation-2' }], nextCursor: null }),
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.select(OTHER);
  await Promise.resolve();
  await Promise.resolve();
  finishFirst({ entries: [{ ...ENTRY, message: 'late' }], nextCursor: null });
  await Promise.resolve();
  expect(controller.getSnapshot().entries[OTHER].messages.map(message => message.text)).toEqual(['hi']);
});

it('reports a history failure with its wire code and request id', async () => {
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async view => view === 'inbox'
      ? [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }]
      : [],
    conversations: listing([CONVERSATION, TARGET]),
    history: async () => {
      throw new HttpError('UPSTREAM_UNAVAILABLE', 'refused', 502, {
        method: 'conversations.history', requestId: 'read-7', status: 502, dispatch: 'not-dispatched',
        catalogDigest: 'sha256:test', attempts: 1, startedAt: 1, durationMs: 1,
      });
    },
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  await Promise.resolve();
  await Promise.resolve();

  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    conversationId: CONVERSATION, historyStatus: 'error',
    historyError: '历史消息读取失败：UPSTREAM_UNAVAILABLE（请求 read-7）',
  });
});

it('starts a new conversation by naming none and adopts the id the daemon returns', async () => {
  const { storage } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) {
      intents.push(params);
      return { ok: true, operationId: params.idempotencyKey, conversationId: params.conversationId ?? 'dc.default',
        messageId: `sent-${intents.length}`, deliveries: [{ target: TARGET, messageId: `sent-${intents.length}` }] };
    },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(7) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  expect(controller.getSnapshot().entries[TARGET].historyStatus).toBe('new');
  await controller.send(TARGET, 'first');
  expect(intents).toHaveLength(1);
  expect(intents[0]).toMatchObject({ to: [TARGET], message: 'first' });
  expect(intents[0]).not.toHaveProperty('conversationId');
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    conversationId: 'dc.default',
    historyStatus: 'unread',
  });
  expect(controller.getSnapshot().entries[TARGET].notice).toBeUndefined();
  await controller.send(TARGET, 'second');
  expect(intents[1]).toMatchObject({ to: [TARGET], conversationId: 'dc.default', message: 'second' });
  expect(controller.getSnapshot().entries[TARGET].messages.map(message => message.text)).toEqual(['first', 'second']);
});

it('dispatches one accepted send and clears the draft without claiming delivery', async () => {
  const { storage, values } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) {
      intents.push(params);
      return { ok: true, operationId: params.idempotencyKey, conversationId: params.conversationId,
        messageId: 'sent-1', deliveries: [{ target: TARGET, messageId: 'sent-1' }] };
    },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(7) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [ENTRY], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello');
  await controller.send(TARGET, 'hello');

  expect(intents).toHaveLength(1);
  expect(intents[0]).toMatchObject({ to: [TARGET], conversationId: CONVERSATION, message: 'hello' });
  expect(controller.getSnapshot().drafts[TARGET]).toBe('');
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    historyStatus: 'unread',
    operation: { status: 'success', canRetry: false },
    notice: undefined,
  });
  expect(controller.getSnapshot().entries[TARGET].messages.at(-1)).toMatchObject({
    author: 'user', text: 'hello', delivery: 'sent',
  });
  expect(values.size).toBe(0);
});

it('keeps the recovery key and does not resend after a lost response', async () => {
  const { storage, values } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const lookups: string[] = [];
  const send = createChatSendApi({
    async send(params) { intents.push(params); throw new Error('lost response'); },
    async keyLookup(params) { lookups.push(params.operationKey); return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(9) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello');
  await controller.send(TARGET, 'hello');

  expect(intents).toHaveLength(1);
  expect(controller.getSnapshot().entries[TARGET]).toMatchObject({
    operation: { status: 'unknown', canRetry: false },
    notice: '发送结果未知；不会自动重发，可用该操作键手动查询。',
  });
  expect(controller.getSnapshot().drafts[TARGET]).toBe('hello');
  expect(values.size).toBe(1);

  await controller.send(TARGET, 'hello again');
  expect(intents).toHaveLength(1);

  await controller.recover();
  expect(lookups).toHaveLength(1);
  expect(values.size).toBe(1);
  expect(controller.getSnapshot().entries[TARGET].operation?.status).toBe('unknown');
});

it('settles a declared admission refusal as a definitive failure without a recovery key', async () => {
  const { storage, values } = memoryStorage();
  const address = 'agent:owner:node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa';
  const send = createChatSendApi({
    async send() {
      throw new DomainError('AGENT_ADDRESS_RETIRED', 'retired', { target: TARGET, address, agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa' }, {
        method: 'message.send', requestId: 'request-1', status: 409, dispatch: 'dispatched',
        catalogDigest: null, attempts: 1, startedAt: Date.now(), durationMs: 1,
      });
    },
    async keyLookup() { throw new Error('unexpected lookup'); },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(4) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello');
  await controller.send(TARGET, 'hello');

  const entry = controller.getSnapshot().entries[TARGET];
  expect(entry.operation).toMatchObject({ status: 'definitive-failure', canRetry: false });
  expect(entry.notice).toContain(address);
  expect(controller.getSnapshot().recoveryPending).toBe(false);
  expect(controller.getSnapshot().drafts[TARGET]).toBe('hello');
  expect(values.size).toBe(0);
});

it('labels targets by displayName, otherwise by the full targetUri', async () => {
  const idTarget = 'agent:owner:node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa';
  const bareTarget = 'agent:owner:node:a.bbbbbbbbbbbbbbbbbbbbbbbbbb';
  const controller = new ConnectedChatController({
    targets: async () => [
      row(idTarget, { agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa', displayName: 'reviewer' }),
      row(bareTarget, { displayName: null }),
      row(TARGET, { displayName: null }),
    ],
    query: async () => [],
    history: async () => ({ entries: [], nextCursor: null }),
    identity: 'user:owner',
  });
  await controller.start();

  expect(controller.getSnapshot().targets.map(target => target.displayName))
    .toEqual(['reviewer', bareTarget, TARGET]);
});

it('keeps an unsettled key actionable on the page without a selected target', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  const intents: ChatSendParams[] = [];
  const lookups: string[] = [];
  const send = createChatSendApi({
    async send(params) { intents.push(params); return {}; },
    async keyLookup(params) { lookups.push(params.operationKey); return { status: 'unknown', reason: 'not-found' }; },
  }, { storage });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();

  expect(lookups).toEqual([key]);
  expect(intents).toEqual([]);
  expect(controller.getSnapshot()).toMatchObject({
    recoveryPending: true, notice: '无法确认该操作是否已持久接受；不会自动重发。',
  });

  controller.select(TARGET);
  await controller.send(TARGET, 'blocked');
  expect(intents).toEqual([]);
  expect(values.get('hyprial.gui.chat.send.operationKey')).toBe(key);
});

it('abandons the local key only when the owner asks, without claiming the send failed', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) {
      intents.push(params);
      return { ok: true, operationId: params.idempotencyKey, conversationId: params.conversationId,
        messageId: 'sent-2', deliveries: [{ target: TARGET, messageId: 'sent-2' }] };
    },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(3) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  expect(controller.getSnapshot().recoveryPending).toBe(true);

  controller.abandon();
  expect(values.size).toBe(0);
  expect(controller.getSnapshot()).toMatchObject({
    recoveryPending: false,
    notice: '已放弃本地恢复键；本次没有重发，也不代表那次发送失败。',
  });

  controller.select(TARGET);
  await controller.send(TARGET, 'hello');
  expect(intents).toHaveLength(1);
});

it('clears the pending recovery flag when the key lookup is durably accepted', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  const send = createChatSendApi({
    async send() { return {}; },
    async keyLookup() {
      return { status: 'accepted', commandId: key, conversationId: CONVERSATION, messageId: 'message-1' };
    },
  }, { storage });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [ENTRY], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();

  expect(values.size).toBe(0);
  expect(controller.getSnapshot().recoveryPending).toBe(false);
  expect(controller.getSnapshot().entries[TARGET]?.notice)
    .toBe('该操作键已有持久发送记录；不表示对方收到，也不能证明原文与丢失输入完全一致。');
});

it('clears a selected target unknown operation on explicit abandon and keeps the draft', async () => {
  const { storage, values } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) { intents.push(params); throw new Error('lost response'); },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(11) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [ENTRY], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello');
  await controller.send(TARGET, 'hello');

  expect(intents).toHaveLength(1);
  expect(controller.getSnapshot().entries[TARGET].operation?.status).toBe('unknown');
  expect(values.size).toBe(1);

  controller.abandon();
  expect(values.size).toBe(0);
  expect(controller.getSnapshot()).toMatchObject({
    recoveryPending: false,
    notice: '已放弃本地恢复键；本次没有重发，也不代表那次发送失败。',
    drafts: { [TARGET]: 'hello' },
  });
  expect(controller.getSnapshot().entries[TARGET].operation).toBeUndefined();
  expect(intents).toHaveLength(1);
});

it('ignores a late unknown lookup that resolves after the owner abandons the key', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  let finishLookup!: (value: unknown) => void;
  let markStarted!: () => void;
  const lookupStarted = new Promise<void>(resolve => { markStarted = resolve; });
  const send = createChatSendApi({
    async send() { return {}; },
    keyLookup: () => new Promise(resolve => { finishLookup = resolve; markStarted(); }),
  }, { storage });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET)],
    query: async () => [{ messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 }],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  const started = controller.start();
  await lookupStarted;
  expect(controller.getSnapshot().recoveryPending).toBe(true);

  controller.abandon();
  expect(values.size).toBe(0);
  finishLookup({ status: 'unknown', reason: 'not-found' });
  await started;

  expect(controller.getSnapshot().recoveryPending).toBe(false);
  expect(controller.getSnapshot().notice)
    .toBe('已放弃本地恢复键；本次没有重发，也不代表那次发送失败。');
  expect(values.size).toBe(0);
});

it('clears another target abandoned unknown operation so returning to it can send again', async () => {
  const { storage, values } = memoryStorage();
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) {
      intents.push(params);
      if (params.to[0] === TARGET) throw new Error('lost response');
      return { ok: true, operationId: params.idempotencyKey, conversationId: params.conversationId,
        messageId: 'sent-2', deliveries: [{ target: params.to[0], messageId: 'sent-2' }] };
    },
    async keyLookup() { return { status: 'unknown', reason: 'not-found' }; },
  }, { storage, randomBytes: () => new Uint8Array(32).fill(7) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async () => [
      { messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 },
      { messageId: 'message-2', conversationId: 'conversation-2', from: 'user:owner', to: OTHER, createdAtMs: 2 },
    ],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello A');
  await controller.send(TARGET, 'hello A');
  expect(intents).toHaveLength(1);
  expect(controller.getSnapshot().entries[TARGET]?.operation?.status).toBe('unknown');
  expect(controller.getSnapshot().recoveryPending).toBe(true);

  controller.setDraft(OTHER, 'hello B');
  controller.select(OTHER);
  await controller.send(OTHER, 'hello B');
  expect(intents).toHaveLength(1);
  expect(controller.getSnapshot().entries[OTHER]?.operation).toMatchObject({
    status: 'not-sent', error: '本标签页还有一个未结算的发送操作键；请先查询或放弃该操作键。',
  });

  controller.abandon();

  expect(values.size).toBe(0);
  expect(controller.getSnapshot().recoveryPending).toBe(false);
  expect(controller.getSnapshot().entries[TARGET]?.operation).toBeUndefined();
  expect(controller.getSnapshot().entries[TARGET]?.notice)
    .toBe('已放弃本地恢复键；本次没有重发，也不代表那次发送失败。');
  expect(controller.getSnapshot().entries[OTHER]?.operation).toBeUndefined();
  expect(controller.getSnapshot().entries[OTHER]?.notice)
    .toBe('已放弃本地恢复键；本次没有重发，也不代表那次发送失败。');
  expect(controller.getSnapshot().drafts).toEqual({ [TARGET]: 'hello A', [OTHER]: 'hello B' });

  controller.select(TARGET);
  expect(intents).toHaveLength(1);
  await controller.send(TARGET, 'hello A');
  expect(intents).toHaveLength(2);
  expect(intents[1]).toMatchObject({ to: [TARGET], conversationId: CONVERSATION, message: 'hello A' });
});

it('drops a late key lookup that resolves after another target abandons the key', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  let finishLookup!: (value: unknown) => void;
  let markStarted!: () => void;
  const lookupStarted = new Promise<void>(resolve => { markStarted = resolve; });
  const send = createChatSendApi({
    async send() { return {}; },
    keyLookup: () => new Promise(resolve => { finishLookup = resolve; markStarted(); }),
  }, { storage });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async () => [
      { messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 },
      { messageId: 'message-2', conversationId: 'conversation-2', from: 'user:owner', to: OTHER, createdAtMs: 2 },
    ],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  const started = controller.start();
  await lookupStarted;
  expect(controller.getSnapshot().recoveryPending).toBe(true);

  controller.setDraft(TARGET, 'hello A');
  controller.select(TARGET);
  controller.setDraft(OTHER, 'hello B');
  controller.select(OTHER);
  controller.abandon();
  finishLookup({ status: 'unknown', reason: 'not-found' });
  await started;

  const snapshot = controller.getSnapshot();
  expect(values.size).toBe(0);
  expect(snapshot.recoveryPending).toBe(false);
  expect(snapshot.entries[TARGET]?.operation).toBeUndefined();
  expect(snapshot.entries[OTHER]?.operation).toBeUndefined();
  expect(snapshot.notice).toBe('已放弃本地恢复键；本次没有重发，也不代表那次发送失败。');
  expect(snapshot.drafts).toEqual({ [TARGET]: 'hello A', [OTHER]: 'hello B' });
});

it('projects a late unknown lookup onto the target that owns the key, not the one selected later', async () => {
  const { storage, values } = memoryStorage();
  let finishLookup!: (value: unknown) => void;
  let markLookup!: () => void;
  const lookupStarted = new Promise<void>(resolve => { markLookup = resolve; });
  const intents: ChatSendParams[] = [];
  const send = createChatSendApi({
    async send(params) { intents.push(params); throw new Error('lost response'); },
    keyLookup: () => new Promise(resolve => { finishLookup = resolve; markLookup(); }),
  }, { storage, randomBytes: () => new Uint8Array(32).fill(7) });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async () => [
      { messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 },
      { messageId: 'message-2', conversationId: 'conversation-2', from: 'user:owner', to: OTHER, createdAtMs: 2 },
    ],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  controller.select(TARGET);
  controller.setDraft(TARGET, 'hello A');
  await controller.send(TARGET, 'hello A');
  expect(intents).toHaveLength(1);
  expect(controller.getSnapshot().entries[TARGET]?.operation?.status).toBe('unknown');

  const recovering = controller.recover();
  await lookupStarted;
  controller.setDraft(OTHER, 'hello B');
  controller.select(OTHER);
  finishLookup({ status: 'unknown', reason: 'not-found' });
  await recovering;

  const snapshot = controller.getSnapshot();
  expect(values.size).toBe(1);
  expect(snapshot.recoveryPending).toBe(true);
  expect(snapshot.activeTargetId).toBe(OTHER);
  expect(snapshot.entries[TARGET]?.notice).toBe('无法确认该操作是否已持久接受；不会自动重发。');
  expect(snapshot.entries[OTHER]?.notice).toBeUndefined();
  expect(snapshot.drafts).toEqual({ [TARGET]: 'hello A', [OTHER]: 'hello B' });
  expect(intents).toHaveLength(1);
});

it('settles a late accepted lookup on its own target and drops the recovery notice', async () => {
  const key = 'k'.repeat(43);
  const { storage, values } = memoryStorage({ 'hyprial.gui.chat.send.operationKey': key });
  const answers: unknown[] = [
    { status: 'unknown', reason: 'not-found' },
    { status: 'accepted', commandId: key, conversationId: CONVERSATION, messageId: 'message-1' },
  ];
  const send = createChatSendApi({
    async send() { return {}; },
    async keyLookup() { return answers.shift(); },
  }, { storage });
  const controller = new ConnectedChatController({
    targets: async () => [row(TARGET), row(OTHER)],
    query: async () => [
      { messageId: 'message-1', conversationId: CONVERSATION, from: 'user:owner', to: TARGET, createdAtMs: 1 },
      { messageId: 'message-2', conversationId: 'conversation-2', from: 'user:owner', to: OTHER, createdAtMs: 2 },
    ],
    history: async () => ({ entries: [], nextCursor: null }),
    send,
    identity: 'user:owner',
  });
  await controller.start();
  expect(controller.getSnapshot()).toMatchObject({
    recoveryPending: true, notice: '无法确认该操作是否已持久接受；不会自动重发。',
  });

  controller.select(OTHER);
  await controller.recover();

  const snapshot = controller.getSnapshot();
  expect(values.size).toBe(0);
  expect(snapshot.recoveryPending).toBe(false);
  expect(snapshot.activeTargetId).toBe(TARGET);
  expect(snapshot.entries[TARGET]?.operation?.status).toBe('success');
  expect(snapshot.notice).toBeUndefined();
});
