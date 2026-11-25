import { ChatController } from '../../src/features/agent-chat';
import { createAgentChatMock } from '../../src/dev/mock/agent-chat';
import { CHAT_FIXTURE_TARGET } from '../../src/dev/fixtures/chat-channels/directory';

it.each(['success', 'failure', 'unknown'] as const)('%s keeps the never-connected Reviewer empty and unable to send', async scenario => {
  const port = createAgentChatMock(scenario);
  const session = await port.call('sessions.create', { targetUri: 'agent:fixture@example.test:fixture-node:reviewer' }) as { sessionId: string };
  expect(await port.call('sessions.history', session)).toEqual({ events: [], nextCursor: null });
  await expect(port.call('sessions.send', { ...session, request: { text: 'blocked', idempotencyKey: 'offline' } })).rejects.toThrow();
  expect(await port.call('sessions.history', session)).toEqual({ events: [], nextCursor: null });
});

it.each(['success', 'unknown'] as const)('runs %s through the real controller without resending', async scenario => {
  const port = createAgentChatMock(scenario);
  const call = vi.spyOn(port, 'call');
  const c = new ChatController(port, () => 'same-key');
  await c.start();
  if (scenario === 'unknown') await c.reconcile(CHAT_FIXTURE_TARGET);
  c.setDraft(CHAT_FIXTURE_TARGET, 'hello');
  await c.send(CHAT_FIXTURE_TARGET, 'hello');
  if (scenario === 'unknown') {
    expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation?.status).toBe('unknown');
    await c.retry('same-key');
    await Promise.all([c.reconcile(CHAT_FIXTURE_TARGET), c.reconcile(CHAT_FIXTURE_TARGET)]);
  }
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation).toMatchObject({ status: 'success', requestId: 'same-key', confirmationPending: false });
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.filter(m => m.text === 'hello')).toHaveLength(1);
  expect(call.mock.calls.filter(([op]) => op === 'sessions.send')).toHaveLength(1);
  expect(call.mock.calls.filter(([op]) => op === 'sessions.send.confirm')).toHaveLength(scenario === 'unknown' ? 2 : 1);
});
it('keeps definite failure and draft without inventing a reply', async () => {
  const c = new ChatController(createAgentChatMock('failure'));
  await c.start(); c.setDraft(CHAT_FIXTURE_TARGET, 'hello'); await c.send(CHAT_FIXTURE_TARGET, 'hello');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation?.status).toBe('definitive-failure');
  expect(c.getSnapshot().drafts[CHAT_FIXTURE_TARGET]).toBe('hello');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages).toHaveLength(2);
});
it.each(['empty', 'error', 'unconfigured'] as const)('does not fall back from %s', async scenario => {
  const c = new ChatController(createAgentChatMock(scenario)); await c.start();
  expect(c.getSnapshot().targets).toHaveLength(0);
  expect(c.getSnapshot().status).toBe(scenario === 'empty' ? 'ready' : 'error');
});
it('rejects malformed or mismatched original requests', async () => {
  const port = createAgentChatMock('success');
  await expect(port.call('sessions.send', { sessionId: 'fixture-session', request: {} })).rejects.toThrow();
  await port.call('sessions.send', { sessionId: 'fixture-session', request: { text: 'original', idempotencyKey: 'key' } });
  await expect(port.call('sessions.send.lookup', { sessionId: 'fixture-session', request: { text: 'changed', idempotencyKey: 'key' } })).rejects.toThrow();
  await expect(port.call('sessions.send.confirm', { sessionId: 'fixture-session', request: { text: 'changed', idempotencyKey: 'key' } })).rejects.toThrow();
});

it('seeds a completed success exchange and returns replies only from host history', async () => {
  const c = new ChatController(createAgentChatMock('success'), () => 'reply-key'); await c.start();
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.map(m => m.author)).toEqual(['user', 'assistant']);
  await c.send(CHAT_FIXTURE_TARGET, '测试回复');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.map(m => m.text)).toEqual([
    '请确认今天的演示安排。', 'FIXTURE · 安排已确认：先检查连接，再开始对话。',
    '测试回复', 'FIXTURE · 已收到：测试回复',
  ]);
});
it('recovers an unknown original request without resending or fabricating a reply', async () => {
  const port = createAgentChatMock('unknown'); const call = vi.spyOn(port, 'call');
  const c = new ChatController(port); await c.start();
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation).toMatchObject({ status: 'unknown', requestId: 'fixture-original-unknown', canRetry: false });
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.map(m => m.text)).toEqual(['请查询上一条消息是否被接收。']);
  await c.send(CHAT_FIXTURE_TARGET, '禁止另发'); await c.retry('fixture-original-unknown');
  expect(call.mock.calls.filter(([op]) => op === 'sessions.send')).toHaveLength(0);
  await c.reconcile(CHAT_FIXTURE_TARGET);
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation?.status).toBe('success');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages).toHaveLength(1);
});

it('keeps consecutive exchanges in host order even when request keys sort backwards', async () => {
  const keys = ['z-first', 'a-second'];
  const c = new ChatController(createAgentChatMock('success'), () => keys.shift()!);
  await c.start();
  await c.send(CHAT_FIXTURE_TARGET, '第一条');
  await c.send(CHAT_FIXTURE_TARGET, '第二条');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.slice(2).map(m => m.text)).toEqual([
    '第一条', 'FIXTURE · 已收到：第一条', '第二条', 'FIXTURE · 已收到：第二条',
  ]);
});

it('partitions sessions, history and request recovery across four targets', async () => {
  const port = createAgentChatMock('unknown');
  const { targets } = await port.call('targets.list') as { targets: { targetUri: string }[] };
  expect(targets).toHaveLength(4);
  const peer = await port.call('sessions.create', { targetUri: targets[1].targetUri }) as { sessionId: string };
  expect(peer.sessionId).not.toBe('fixture-session');
  const request = { text: '请查询上一条消息是否被接收。', idempotencyKey: 'fixture-original-unknown' };
  expect(await port.call('sessions.send.lookup', { sessionId: peer.sessionId, request })).toEqual({ status: 'unknown' });
  expect(await port.call('sessions.history', { sessionId: peer.sessionId })).toEqual({ events: [], nextCursor: null });
  await expect(port.call('sessions.send', { sessionId: 'invented-session', request })).rejects.toThrow();
  const offline = await port.call('sessions.create', { targetUri: targets[3].targetUri }) as { sessionId: string };
  await expect(port.call('sessions.send', { sessionId: offline.sessionId, request })).rejects.toThrow();
});

it('accepts identical keys independently and keeps target histories distinct', async () => {
  const port = createAgentChatMock('success');
  const { targets } = await port.call('targets.list') as { targets: { targetUri: string }[] };
  expect(targets).toHaveLength(4);
  const peer = await port.call('sessions.create', { targetUri: targets[1].targetUri }) as { sessionId: string; conversationId: string };
  expect(peer.conversationId).not.toBe('fixture-conversation');
  const request = { text: 'primary only', idempotencyKey: 'same-key' };
  await port.call('sessions.send', { sessionId: 'fixture-session', request });
  expect(await port.call('sessions.send.lookup', { sessionId: peer.sessionId, request })).toEqual({ status: 'unknown' });
  await port.call('sessions.send', { sessionId: peer.sessionId, request: { ...request, text: 'peer only' } });
  const primary = await port.call('sessions.history', { sessionId: 'fixture-session' }) as { events: { text: string; at: string }[] };
  const secondary = await port.call('sessions.history', { sessionId: peer.sessionId }) as typeof primary;
  expect(primary.events.some(e => e.text === 'peer only')).toBe(false);
  expect(secondary.events.some(e => e.text === 'primary only')).toBe(false);
  for (const history of [primary, secondary]) expect(history.events.every((e, i, a) => !i || e.at > a[i - 1].at)).toBe(true);
});

it('restores each controller draft and reconciles only the selected target request', async () => {
  const c = new ChatController(createAgentChatMock('unknown'), () => 'peer-unknown');
  await c.start();
  const peer = c.getSnapshot().targets[1].id;
  c.setDraft(CHAT_FIXTURE_TARGET, 'primary draft');
  await c.select(peer);
  expect(c.getSnapshot().entries[peer].operation).toBeUndefined();
  c.setDraft(peer, 'peer draft');
  await c.send(peer, 'peer message');
  expect(c.getSnapshot().entries[peer].operation?.status).toBe('unknown');
  await c.reconcile(peer);
  expect(c.getSnapshot().entries[peer].operation?.status).toBe('success');
  await c.select(CHAT_FIXTURE_TARGET);
  expect(c.getSnapshot().drafts[CHAT_FIXTURE_TARGET]).toBe('primary draft');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation?.requestId).toBe('fixture-original-unknown');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].operation?.status).toBe('unknown');
  expect(c.getSnapshot().entries[CHAT_FIXTURE_TARGET].messages.some(m => m.text === 'peer message')).toBe(false);
});
