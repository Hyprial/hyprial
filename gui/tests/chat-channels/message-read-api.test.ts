import { createGeneratedClient } from '../../src/client/generated';
import {
  createGroupChatReadApiFromClient,
  createMessageReadApi,
  createMessageReadApiFromClient,
} from '../../src/features/chat-channels';

const accepted = { status: 'accepted', commandId: 'key-1', conversationId: 'conv-1', messageId: 'msg-1' };

it('shares one host supplied generated client across chat reads', async () => {
  const methods: string[] = [];
  const client = createGeneratedClient({ invoke: async <T>(method: string): Promise<T> => {
    methods.push(method);
    return { ok: true, conversationId: 'c.conv-1', entries: [], nextCursor: null } as T;
  } });
  const messages = createMessageReadApiFromClient(client);
  expect(await messages.history('c.conv-1')).toEqual({ entries: [], nextCursor: null });
  expect(methods).toEqual(['conversations.history']);
});

it('rejects an unavailable message method before dispatch while retaining sibling reads', async () => {
  const methods: string[] = [];
  const client = createGeneratedClient({ invoke: async <T>(method: string): Promise<T> => {
    methods.push(method);
    return { ok: true, conversationId: 'c.conv-1', entries: [], nextCursor: null } as T;
  } });
  const messages = createMessageReadApiFromClient(client, (method) => method === 'message.query'
    ? { available: false, code: 'METHOD_NOT_FOUND', reason: '目录查询暂不可用。' }
    : { available: true });
  await expect(messages.history('c.conv-1')).resolves.toEqual({ entries: [], nextCursor: null });
  await expect(messages.query('outbox')).rejects.toMatchObject({
    name: 'MethodRefusedError', method: 'message.query', code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched',
    message: 'METHOD_NOT_FOUND: message.query',
  });
  expect(methods).toEqual(['conversations.history']);
});

it('uses declared message read methods and keeps an unknown send unresolved', async () => {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const api = createMessageReadApi({ request: async (method, params) => {
    calls.push({ method, params });
    if (method === 'message.send.lookup') return { status: 'unknown', reason: 'not-found' };
    if (method === 'conversations.history') return { ok: true, conversationId: 'c.cell', entries: [], nextCursor: null };
    if (method === 'conversations.list') return { ok: true, participant: 'user:owner', conversations: [
      { conversationId: 'c.cell', legacyConversationId: 'conv-1', peer: 'agent:owner:node:worker', lastMessageAtMs: 5 },
    ], nextCursor: 'next' };
    return { ok: true, actor: 'user:owner', view: 'outbox', entries: [] };
  } });
  expect(await api.lookup({ target: 'agent:owner:node:worker', text: 'hello', conversationId: 'conv-1', idempotencyKey: 'key-1' }))
    .toEqual({ status: 'unknown', reason: 'not-found' });
  expect(await api.history('c.cell')).toEqual({ entries: [], nextCursor: null });
  expect(await api.conversations('cursor')).toEqual({ entries: [
    { documentId: 'c.cell', conversationId: 'conv-1', peer: 'agent:owner:node:worker', lastMessageAtMs: 5 },
  ], nextCursor: 'next' });
  expect(await api.query('outbox')).toEqual([]);
  expect(calls).toEqual([
    { method: 'message.send.lookup', params: { target: 'agent:owner:node:worker', text: 'hello', conversationId: 'conv-1', idempotencyKey: 'key-1' } },
    { method: 'conversations.history', params: { conversationId: 'c.cell' } },
    { method: 'conversations.list', params: { cursor: 'cursor' } },
    { method: 'message.query', params: { view: 'outbox' } },
  ]);
});

it('accepts only matching confirmed lookups and keeps malformed responses unconfirmed', async () => {
  const request = { target: 'agent:owner:node:worker', text: 'hello', conversationId: 'conv-1', idempotencyKey: 'key-1' };
  const valid = createMessageReadApi({ request: async () => accepted });
  expect(await valid.lookup(request)).toEqual(accepted);
  for (const value of [{ ...accepted, commandId: 'other' }, { status: 'success' }, { status: 'unknown' }]) {
    const api = createMessageReadApi({ request: async () => value });
    await expect(api.lookup(request)).rejects.toThrow();
  }
});

it('rejects mismatched history, conversation and query envelopes', async () => {
  const history = createMessageReadApi({ request: async () => ({ ok: true, conversationId: 'wrong', entries: [], nextCursor: null }) });
  await expect(history.history('c.conv-1')).rejects.toThrow();
  const query = createMessageReadApi({ request: async () => ({ ok: true, actor: 'user:owner', view: 'inbox', entries: [] }) });
  await expect(query.query('outbox')).rejects.toThrow();
});

it('separates group heads from direct heads when kinds includes group and validates group entry fields', async () => {
  const calls: Array<{ method: string; params: unknown }> = [];
  const client = createGeneratedClient({ invoke: async <T>(method: string, params: unknown): Promise<T> => {
    calls.push({ method, params });
    if (method === 'conversations.list') {
      return {
        ok: true,
        participant: 'user:alice',
        nextCursor: null,
        recordAvailable: true,
        conversations: [
          {
            kind: 'group',
            conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
            legacyConversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
            title: 'release',
            memberCount: 3,
            lastMessageAtMs: 20,
            lastMessageId: 'm-2',
            messages: 1,
            messageClass: 'interaction',
            threadId: null,
          },
          {
            conversationId: 'c.cell',
            legacyConversationId: 'conv-1',
            kind: 'agent',
            peer: 'agent:alice:node:worker',
            threadId: null,
            lastMessageAtMs: 10,
            lastMessageId: 'm-1',
            messages: 1,
          },
        ],
      } as T;
    }
    return {
      ok: true,
      conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
      legacyConversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
      threadId: null,
      nextCursor: null,
      memberNames: {
        'user:alice': 'alice',
        'a.worker': 'worker',
        'a.former': 'former',
      },
      entries: [
        {
          kind: 'message',
          messageId: 'm-sys',
          conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
          from: 'user:alice',
          to: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
          intent: 'request',
          createdAtMs: 1,
          message: 'alice created the group "release"',
          origin: {},
          messageClass: 'system',
          system: { event: 'created', memberId: 'user:alice' },
        },
        {
          kind: 'message',
          messageId: 'm-msg',
          conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
          from: 'user:alice',
          to: 'g.4lvgrfdnb4vtmpxk36wa55phb4',
          intent: 'reply',
          createdAtMs: 2,
          message: 'please tag',
          origin: {},
          messageClass: 'interaction',
          replyTo: { messageId: 'm-prev', from: 'a.worker', excerpt: 'cut release' },
          mentions: ['a.worker'],
        },
      ],
    } as T;
  } });

  const read = createMessageReadApiFromClient(client);
  const listed = await read.conversations(undefined, ['direct', 'group']);
  expect(listed).toEqual({
    entries: [{ documentId: 'c.cell', conversationId: 'conv-1', peer: 'agent:alice:node:worker', lastMessageAtMs: 10 }],
    groups: [{ conversationId: 'g.4lvgrfdnb4vtmpxk36wa55phb4', title: 'release', memberCount: 3, lastMessageAtMs: 20 }],
    nextCursor: null,
    recordAvailable: true,
  });
  expect(calls[0].params).toEqual({ kinds: ['direct', 'group'] });

  const groupRead = createGroupChatReadApiFromClient(client);
  const page = await groupRead.history('g.4lvgrfdnb4vtmpxk36wa55phb4', undefined, 'tail');
  expect(page.entries).toHaveLength(2);
  expect(page.memberNames).toEqual({
    'user:alice': 'alice',
    'a.worker': 'worker',
    'a.former': 'former',
  });
  expect(page.entries[0].system).toEqual({ event: 'created', memberId: 'user:alice' });
  expect(page.entries[1].replyTo).toEqual({ messageId: 'm-prev', from: 'a.worker', excerpt: 'cut release' });
  expect(page.entries[1].mentions).toEqual(['a.worker']);
});

it('rejects malformed group heads, invalid memberNames and invalid group entry metadata', async () => {
  const badHead = createGeneratedClient({ invoke: async <T>(): Promise<T> => ({
    ok: true,
    participant: 'user:alice',
    nextCursor: null,
    conversations: [{ kind: 'group', conversationId: 'g.one', title: '', memberCount: 0, lastMessageAtMs: 1 }],
  }) as T });
  await expect(createMessageReadApiFromClient(badHead).conversations(undefined, ['direct', 'group'])).rejects.toThrow('会话目录格式不正确');

  const badSystem = createGeneratedClient({ invoke: async <T>(): Promise<T> => ({
    ok: true,
    conversationId: 'g.one',
    nextCursor: null,
    entries: [{
      kind: 'message', messageId: 'm1', conversationId: 'g.one', from: 'user:alice', to: 'g.one',
      intent: 'request', createdAtMs: 1, message: 'x', origin: {}, system: { event: 'unknown', memberId: 'user:alice' },
    }],
  }) as T });
  await expect(createGroupChatReadApiFromClient(badSystem).history('g.one')).rejects.toThrow('历史消息格式不正确');

  const badMemberNames = createGeneratedClient({ invoke: async <T>(): Promise<T> => ({
    ok: true,
    conversationId: 'g.one',
    nextCursor: null,
    memberNames: ['bad'],
    entries: [],
  }) as T });
  await expect(createGroupChatReadApiFromClient(badMemberNames).history('g.one')).rejects.toThrow('历史消息成员名称表格式不正确');
});

it('refuses group history before dispatch when method access rejects it', async () => {
  const calls: string[] = [];
  const client = createGeneratedClient({ invoke: async <T>(method: string): Promise<T> => {
    calls.push(method);
    return { ok: true } as T;
  } });
  const groupRead = createGroupChatReadApiFromClient(client, () => ({ available: false, code: 'METHOD_NOT_FOUND' }));
  await expect(groupRead.history('g.one')).rejects.toMatchObject({
    name: 'MethodRefusedError',
    method: 'conversations.history',
    code: 'METHOD_NOT_FOUND',
    dispatch: 'not-dispatched',
  });
  expect(calls).toEqual([]);
});
