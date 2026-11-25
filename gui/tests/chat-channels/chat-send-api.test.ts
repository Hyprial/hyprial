/**
 * Decision record (chat C1 frozen-contract send adapter tests, 2026-11-05)
- The public registry/OpenAPI/generated client do not yet declare message.send
  or message.send.key_lookup, so these tests drive the adapter through a frozen
  contract double that mirrors the merged declaration exactly. A passing test
  here is adapter evidence, not real daemon or Contract Mock evidence.
 */

import { DomainError, TransportError } from '../../src/client';
import {
  CHAT_SEND_ACCEPTED_NOTICE,
  CHAT_SEND_OPERATION_KEY_NAME,
  CHAT_SEND_PENDING_NOTICE,
  CHAT_SEND_RECOVERY_ACCEPTED_NOTICE,
  CHAT_SEND_RECOVERY_UNKNOWN_NOTICE,
  CHAT_SEND_REFUSED_NOTICE,
  CHAT_SEND_STORE_FAILED_NOTICE,
  CHAT_SEND_UNKNOWN_NOTICE,
  createChatSendApi,
  isCanonicalAgentUri,
  type ChatSendParams,
  type ChatSendStorage,
} from '../../src/features/chat-channels';

const TARGET = 'agent:owner:node:worker';
const CONVERSATION = 'conversation-1';

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map<string, string>(Object.entries(initial));
  const events: string[] = [];
  const storage: ChatSendStorage = {
    getItem(name) { events.push(`read:${name}:${values.get(name) ?? 'null'}`); return values.get(name) ?? null; },
    setItem(name, value) { events.push(`write:${name}:${value}`); values.set(name, value); },
    removeItem(name) { events.push(`remove:${name}`); values.delete(name); },
  };
  return { storage, values, events };
}

function bytes32(fill: number): Uint8Array {
  return new Uint8Array(32).fill(fill);
}

function acceptedSend(params: ChatSendParams) {
  return {
    ok: true,
    actingPrincipal: 'user:owner',
    operationId: params.idempotencyKey,
    conversationId: params.conversationId,
    messageId: 'message-1',
    deliveries: [{ target: TARGET, messageId: 'message-1', accepted: true, queued: false }],
  };
}

function portFixture(overrides: {
  send?: (params: ChatSendParams) => Promise<unknown>;
  keyLookup?: (params: { operationKey: string }) => Promise<unknown>;
} = {}) {
  const calls: Array<{ method: string; params: unknown }> = [];
  return {
    calls,
    port: {
      async send(params: ChatSendParams) {
        calls.push({ method: 'message.send', params });
        return overrides.send ? overrides.send(params) : acceptedSend(params);
      },
      async keyLookup(params: { operationKey: string }) {
        calls.push({ method: 'message.send.key_lookup', params });
        return overrides.keyLookup ? overrides.keyLookup(params) : { status: 'unknown', reason: 'not-found' };
      },
    },
  };
}

it('saves one 32-byte url-safe key before dispatch and clears it on durable acceptance', async () => {
  const { storage, values, events } = memoryStorage();
  const { calls, port } = portFixture({ send: async (params) => {
    expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe(params.idempotencyKey);
    return acceptedSend(params);
  } });
  const api = createChatSendApi(port, { storage, randomBytes: () => bytes32(7) });
  const outcome = await api.send({ to: [TARGET], message: 'hello there', conversationId: CONVERSATION });
  expect(calls).toHaveLength(1);
  expect(outcome.status).toBe('accepted');
  if (outcome.status !== 'accepted') throw new Error('expected acceptance');
  expect(outcome.operationKey).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(outcome.notice).toBe(CHAT_SEND_ACCEPTED_NOTICE);
  expect(outcome.notice).toBe('消息已提交。');
  expect(outcome.notice).not.toContain('消息已送达');
  const sent = calls[0].params as ChatSendParams;
  expect(sent).toEqual({ to: [TARGET], message: 'hello there', summary: 'hello there', conversationId: CONVERSATION, idempotencyKey: outcome.operationKey });
  expect(values.has(CHAT_SEND_OPERATION_KEY_NAME)).toBe(false);
  expect(events[1]).toBe(`write:${CHAT_SEND_OPERATION_KEY_NAME}:${outcome.operationKey}`);
});

it('refuses to dispatch when the key cannot be persisted or read back verbatim', async () => {
  const throwing: ChatSendStorage = {
    getItem: () => null,
    setItem() { throw new Error('quota'); },
    removeItem() { return; },
  };
  const first = portFixture();
  const refused = await createChatSendApi(first.port, { storage: throwing, randomBytes: () => bytes32(1) })
    .send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(refused).toEqual({ status: 'not-started', reason: 'store-failed', notice: CHAT_SEND_STORE_FAILED_NOTICE });
  expect(first.calls).toHaveLength(0);

  const values = new Map<string, string>();
  const rewritten: ChatSendStorage = {
    getItem: (name) => (values.has(name) ? `${values.get(name) ?? ''}x` : null),
    setItem(name, value) { values.set(name, value); },
    removeItem(name) { values.delete(name); },
  };
  const second = portFixture();
  const mismatched = await createChatSendApi(second.port, { storage: rewritten, randomBytes: () => bytes32(2) })
    .send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(mismatched.status).toBe('not-started');
  expect(second.calls).toHaveLength(0);
  expect(values.size).toBe(0);
});

it('keeps the key and refuses a second dispatch when the response is lost', async () => {
  const { storage, values } = memoryStorage();
  const { calls, port } = portFixture({ send: async () => { throw new Error('socket closed'); } });
  const api = createChatSendApi(port, { storage, randomBytes: () => bytes32(3) });
  const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(outcome.status).toBe('unknown');
  if (outcome.status !== 'unknown') throw new Error('expected unknown');
  expect(outcome.notice).toBe(CHAT_SEND_UNKNOWN_NOTICE);
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe(outcome.operationKey);
  expect(calls).toHaveLength(1);
  const again = await api.send({ to: [TARGET], message: 'hello again', conversationId: CONVERSATION });
  expect(again).toEqual({ status: 'not-started', reason: 'pending-operation', notice: CHAT_SEND_PENDING_NOTICE });
  expect(calls).toHaveLength(1);
});

it('treats an unmatched acceptance shape as unknown and keeps the key', async () => {
  const variants: Array<(params: ChatSendParams) => unknown> = [
    params => ({ ...acceptedSend(params), operationId: 'other-key' }),
    params => ({ ...acceptedSend(params), conversationId: 'other-conversation' }),
    params => ({ ...acceptedSend(params), messageId: '' }),
    params => ({ ...acceptedSend(params), ok: false }),
    params => ({ ...acceptedSend(params), deliveries: [] }),
    params => ({ ...acceptedSend(params), deliveries: [
      { target: TARGET, messageId: 'message-1', accepted: true },
      { target: TARGET, messageId: 'message-2', accepted: true },
    ] }),
    params => ({ ...acceptedSend(params), deliveries: [{ target: 'agent:other:node:worker', messageId: 'message-1', accepted: true }] }),
  ];
  for (const [index, variant] of variants.entries()) {
    const { storage, values } = memoryStorage();
    const { calls, port } = portFixture({ send: async (params) => variant(params) });
    const api = createChatSendApi(port, { storage, randomBytes: () => bytes32(index + 4) });
    const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
    expect(outcome.status).toBe('unknown');
    if (outcome.status !== 'unknown') throw new Error('expected unknown');
    expect(outcome.reason).toBe('response-mismatch');
    expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe(outcome.operationKey);
    expect(calls).toHaveLength(1);
  }
});

it('rejects multiple or non-canonical targets before any dispatch and stores nothing', async () => {
  expect(isCanonicalAgentUri(TARGET)).toBe(true);
  for (const value of ['agent:a:b', 'agent:a:b:c:d', 'user:owner', 'route:lark:chat', 'agent::b:c', 'agent:a :b:c', 'agent:a:b:c ']) {
    expect(isCanonicalAgentUri(value)).toBe(false);
  }
  const intents = [
    { to: [TARGET, 'agent:owner:node:other'], message: 'hello', conversationId: CONVERSATION },
    { to: ['user:owner'], message: 'hello', conversationId: CONVERSATION },
    { to: ['route:lark:chat'], message: 'hello', conversationId: CONVERSATION },
    { to: ['agent:a:b'], message: 'hello', conversationId: CONVERSATION },
    { to: [TARGET], message: '   ', conversationId: CONVERSATION },
    { to: [TARGET], message: 'hello', conversationId: '' },
  ];
  for (const intent of intents) {
    const { storage, values } = memoryStorage();
    const { calls, port } = portFixture();
    const api = createChatSendApi(port, { storage, randomBytes: () => bytes32(9) });
    const outcome = await api.send(intent);
    expect(outcome.status).toBe('not-started');
    expect(calls).toHaveLength(0);
    expect(values.size).toBe(0);
  }
});

it('recovers after refresh with only the stored key and opens the recorded conversation', async () => {
  const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  const { calls, port } = portFixture({ keyLookup: async ({ operationKey }) => ({
    status: 'accepted', commandId: operationKey, conversationId: 'conversation-1', messageId: 'message-9',
  }) });
  const api = createChatSendApi(port, { storage });
  const recovery = await api.recover();
  expect(calls).toEqual([{ method: 'message.send.key_lookup', params: { operationKey: 'k'.repeat(43) } }]);
  expect(recovery).toEqual({
    status: 'accepted', operationKey: 'k'.repeat(43), conversationId: 'conversation-1',
    messageId: 'message-9', notice: CHAT_SEND_RECOVERY_ACCEPTED_NOTICE,
  });
  expect(values.has(CHAT_SEND_OPERATION_KEY_NAME)).toBe(false);
});

it('does not erase a replacement identity key when an older request finishes', async () => {
  const { storage, values } = memoryStorage();
  let finishSend!: (value: unknown) => void;
  const { calls, port } = portFixture({ send: () => new Promise(resolve => { finishSend = resolve; }) });
  const api = createChatSendApi(port, { storage, randomBytes: () => bytes32(5) });
  const pending = api.send({ to: [TARGET], message: 'old', conversationId: CONVERSATION });
  await Promise.resolve();
  const oldParams = calls[0].params as ChatSendParams;
  api.clear('identity-change');
  storage.setItem(CHAT_SEND_OPERATION_KEY_NAME, 'n'.repeat(43));
  finishSend(acceptedSend(oldParams));
  expect((await pending).status).toBe('accepted');
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('n'.repeat(43));
});

it('does not erase a replacement identity key when an older lookup finishes', async () => {
  const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
  let finishLookup!: (value: unknown) => void;
  const { port } = portFixture({ keyLookup: () => new Promise(resolve => { finishLookup = resolve; }) });
  const api = createChatSendApi(port, { storage });
  const pending = api.recover();
  await Promise.resolve();
  api.clear('identity-change');
  storage.setItem(CHAT_SEND_OPERATION_KEY_NAME, 'n'.repeat(43));
  finishLookup({ status: 'accepted', commandId: 'k'.repeat(43), conversationId: CONVERSATION, messageId: 'old-message' });
  expect((await pending).status).toBe('accepted');
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('n'.repeat(43));
});

it('keeps the key for not-found, invalid-record, unavailable and unmatched lookups', async () => {
  const replies: unknown[] = [
    { status: 'unknown', reason: 'not-found' },
    { status: 'unknown', reason: 'invalid-record' },
    { status: 'unknown', reason: 'unavailable' },
    { status: 'accepted', commandId: 'other-key', conversationId: 'conversation-1', messageId: 'message-1' },
    { status: 'accepted', commandId: 'k'.repeat(43), conversationId: 'conversation-1', messageId: '' },
    { status: 'success' },
    { status: 'unknown', reason: 'request-mismatch' },
  ];
  for (const reply of replies) {
    const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
    const { port } = portFixture({ keyLookup: async () => reply });
    const recovery = await createChatSendApi(port, { storage }).recover();
    expect(recovery.status).toBe('unknown');
    if (recovery.status !== 'unknown') throw new Error('expected unknown');
    expect(recovery.notice).toBe(CHAT_SEND_RECOVERY_UNKNOWN_NOTICE);
    expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('k'.repeat(43));
  }
  const { storage: idle, values: idleValues } = memoryStorage();
  const { calls, port } = portFixture();
  expect(await createChatSendApi(port, { storage: idle }).recover()).toEqual({ status: 'idle', notice: '没有可恢复的发送操作键。' });
  expect(calls).toHaveLength(0);
  expect(idleValues.size).toBe(0);
});

it('blocks sends and lookups when the declared catalog does not offer the method', async () => {
  const { storage, values } = memoryStorage();
  const first = portFixture();
  const sendRefused = await createChatSendApi(first.port, {
    storage, randomBytes: () => bytes32(5),
    access: (method) => method === 'message.send'
      ? { available: false, code: 'METHOD_NOT_FOUND', reason: '当前后端目录没有声明 message.send。' }
      : { available: true },
  }).send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(sendRefused).toEqual({ status: 'not-started', reason: 'method-refused', code: 'METHOD_NOT_FOUND', source: 'message.send' });
  expect(first.calls).toHaveLength(0);
  expect(values.size).toBe(0);

  values.set(CHAT_SEND_OPERATION_KEY_NAME, 'k'.repeat(43));
  const second = portFixture();
  const recovery = await createChatSendApi(second.port, {
    storage,
    access: () => ({ available: false, reason: '未声明 message.send.key_lookup。' }),
  }).recover();
  expect(recovery.status).toBe('unknown');
  if (recovery.status !== 'unknown') throw new Error('expected unknown');
  expect(recovery.reason).toBe('unavailable');
  expect(second.calls).toHaveLength(0);
  expect(values.get(CHAT_SEND_OPERATION_KEY_NAME)).toBe('k'.repeat(43));
});

it('clears the key on explicit cancel, sign-out, identity change and pre-dispatch transport refusal', async () => {
  for (const reason of ['cancelled', 'sign-out', 'identity-change'] as const) {
    const { storage, values } = memoryStorage({ [CHAT_SEND_OPERATION_KEY_NAME]: 'k'.repeat(43) });
    const api = createChatSendApi(portFixture().port, { storage });
    api.clear(reason);
    expect(values.size).toBe(0);
  }

  const { storage, values } = memoryStorage();
  const attempts: string[] = [];
  const diagnostic = {
    method: 'message.send', requestId: null, status: 401, dispatch: 'not-dispatched' as const,
    catalogDigest: null, attempts: 0, startedAt: Date.now(), durationMs: 0,
  };
  const api = createChatSendApi({
    async send(params) {
      attempts.push(params.idempotencyKey);
      throw new TransportError('configuration', 'BACKEND_CONNECTION_RETIRED', 'retired', diagnostic);
    },
    async keyLookup() { throw new Error('unexpected lookup'); },
  }, { storage, randomBytes: () => bytes32(6) });
  const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(outcome).toEqual({ status: 'not-started', reason: 'capability-unavailable', notice: '发送未开始：BACKEND_CONNECTION_RETIRED。' });
  expect(values.size).toBe(0);
  expect(attempts).toHaveLength(1);
});

it('keeps the key when a rejected dispatch cannot prove the request was never sent', async () => {
  const { storage, values } = memoryStorage();
  const api = createChatSendApi({
    async send() {
      throw new TransportError('network', 'TRANSPORT_NETWORK', 'socket reset', {
        method: 'message.send', requestId: 'request-1', status: null, dispatch: 'unknown',
        catalogDigest: null, attempts: 1, startedAt: Date.now(), durationMs: 3,
      });
    },
    async keyLookup() { throw new Error('unexpected lookup'); },
  }, { storage, randomBytes: () => bytes32(8) });
  const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(outcome.status).toBe('unknown');
  expect(values.size).toBe(1);
});

function dispatchedRefusal(code: string, data: unknown = null, dispatch: 'dispatched' | 'unknown' = 'dispatched') {
  return new DomainError(code, 'refused', data, {
    method: 'message.send', requestId: 'request-9', status: 409, dispatch,
    catalogDigest: null, attempts: 1, startedAt: Date.now(), durationMs: 2,
  });
}

it('settles a dispatched, declared admission refusal as definitive and clears the key', async () => {
  const address = 'agent:owner:node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa';
  const { storage, values } = memoryStorage();
  const sent: string[] = [];
  const api = createChatSendApi({
    async send(params) {
      sent.push(params.idempotencyKey);
      throw dispatchedRefusal('AGENT_ADDRESS_RETIRED', { target: TARGET, address, agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa' });
    },
    async keyLookup() { throw new Error('unexpected lookup'); },
  }, { storage, randomBytes: () => bytes32(9) });
  const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(outcome).toEqual({
    status: 'refused', operationKey: sent[0], reason: 'dispatch-refused', code: 'AGENT_ADDRESS_RETIRED',
    notice: `该 Agent 的名字地址已停用，本次未发送。请改用新地址：${address}`,
  });
  expect(values.size).toBe(0);
  expect(api.pendingOperationKey()).toBeNull();
  // The tab is not blocked: the next intent dispatches with a fresh key.
  const again = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(again.status).toBe('refused');
  expect(sent).toHaveLength(2);
  expect(sent[1]).toBe(sent[0]);
});

it.each([
  ['TARGET_UNRESOLVED', '找不到这个 Agent'],
  ['SESSION_SUPERSEDED', '当前会话已被新的会话取代'],
  ['TARGET_NOT_AUTHORIZED', '没有向该 Agent 发送的权限'],
])('treats a dispatched %s as a definitive refusal without an address hint', async (code, text) => {
  const { storage, values } = memoryStorage();
  const api = createChatSendApi({
    async send() { throw dispatchedRefusal(code, { target: TARGET }); },
    async keyLookup() { throw new Error('unexpected lookup'); },
  }, { storage, randomBytes: () => bytes32(10) });
  const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
  expect(outcome.status).toBe('refused');
  if (outcome.status !== 'refused') throw new Error('expected refusal');
  expect(outcome.code).toBe(code);
  expect(outcome.notice).toContain(text);
  expect(outcome.notice).not.toContain('请改用新地址');
  expect(outcome.notice).not.toBe(CHAT_SEND_REFUSED_NOTICE);
  expect(values.size).toBe(0);
});

it('keeps unknown for an undeclared code, an unknown dispatch or a delivery-time code', async () => {
  for (const error of [
    dispatchedRefusal('DAEMON_ERROR'),
    dispatchedRefusal('AGENT_ADDRESS_RETIRED', null, 'unknown'),
    dispatchedRefusal('USER_DELIVERY_TIMEOUT'),
    dispatchedRefusal('SUBMIT_OUTCOME_UNKNOWN'),
  ]) {
    const { storage, values } = memoryStorage();
    const api = createChatSendApi({
      async send() { throw error; },
      async keyLookup() { throw new Error('unexpected lookup'); },
    }, { storage, randomBytes: () => bytes32(11) });
    const outcome = await api.send({ to: [TARGET], message: 'hello', conversationId: CONVERSATION });
    expect(outcome.status, error.code).toBe('unknown');
    expect(values.size, error.code).toBe(1);
  }
});
