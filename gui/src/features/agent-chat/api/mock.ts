import { LiveError, object, string, type LivePort } from '../../../adapters/live/client';
import { CHAT_FIXTURE_DIRECTORY, CHAT_FIXTURE_TARGET } from '../../../fixtures/chat-directory';
export type AgentChatScenario = 'success' | 'empty' | 'error' | 'unconfigured' | 'unknown' | 'failure';
export { CHAT_FIXTURE_TARGET } from '../../../fixtures/chat-directory';
/** Opt-in in-memory host-contract fixture. Never a daemon or durable journal. */
export function createAgentChatMock(scenario: AgentChatScenario): LivePort {
  const sessions = CHAT_FIXTURE_DIRECTORY.map((target, targetIndex) => {
    const sessionId = targetIndex === 0 ? 'fixture-session' : `fixture-session-${targetIndex}`;
    const conversationId = targetIndex === 0 ? 'fixture-conversation' : `fixture-conversation-${targetIndex}`;
    const seedTexts = target.status !== 'online' ? [] : scenario === 'success'
      ? [['user', '请确认今天的演示安排。'], ['assistant', 'FIXTURE · 安排已确认：先检查连接，再开始对话。']]
      : scenario === 'failure'
        ? [['user', '开始发送前需要检查什么？'], ['assistant', 'FIXTURE · 请检查服务连接；本场景后续发送会被明确拒绝。']]
        : scenario === 'unknown' && targetIndex === 0 ? [['user', '请查询上一条消息是否被接收。']] : [];
    if (targetIndex > 0 && seedTexts.length) {
      seedTexts[0][1] = `请${target.displayName}确认当前任务。`;
      seedTexts[1][1] = `FIXTURE · ${target.displayName} 已整理当前任务。`;
    }
    const eventTime = (index: number) => new Date(Date.parse('2026-10-31T00:00:00Z') + index * 1000).toISOString();
    const events: Record<string, unknown>[] = seedTexts.map(([kind, text], index) => ({
      eventId: `fixture-history-${index}`, kind, text, at: eventTime(index),
    }));
    const accepted = new Map<string, Record<string, unknown>>();
    const originalText = new Map<string, string>();
    const pending = new Map<string, unknown>();
    // Model a lost response using the existing recovery contract: the host accepted
    // the original request, but the client must look up and confirm its exact key.
    if (scenario === 'unknown' && targetIndex === 0) {
      const key = 'fixture-original-unknown', text = '请查询上一条消息是否被接收。';
      originalText.set(key, text);
      accepted.set(key, { status: 'accepted', commandId: key, conversationId, messageId: 'fixture-history-0' });
      pending.set(key, { sessionId, targetUri: target.targetUri, request: { text, idempotencyKey: key }, status: 'unknown' });
    }
    return { target, sessionId, conversationId, events, accepted, originalText, pending, eventTime };
  });
  return { async call(operation, params = {}) {
    if (operation === 'targets.list') {
      if (scenario === 'error') throw new LiveError('fixture-error', 'FIXTURE · 聊天对象读取失败。');
      if (scenario === 'unconfigured') throw new LiveError('unconfigured', 'FIXTURE · 聊天服务未配置。');
      return { targets: scenario === 'empty' ? [] : CHAT_FIXTURE_DIRECTORY.map(target => ({ ...target })), preferredTargetUri: scenario === 'empty' ? null : CHAT_FIXTURE_TARGET };
    }
    if (operation === 'sessions.pending') return { requests: sessions.flatMap(session => [...session.pending.values()]) };
    const session = operation === 'sessions.create'
      ? sessions.find(session => session.target.targetUri === params.targetUri)
      : sessions.find(session => session.sessionId === params.sessionId);
    if (!session) throw new LiveError('fixture-session', 'FIXTURE · 会话不匹配。', true);
    const { target, sessionId, conversationId, events, accepted, originalText, pending, eventTime } = session;
    if (operation === 'sessions.create') return { targetUri: target.targetUri, sessionId, conversationId };
    if (operation === 'sessions.history') return { events: [...events], nextCursor: null };
    const request = object(params.request);
    const key = string(request.idempotencyKey);
    const text = string(request.text);
    if (originalText.has(key) && originalText.get(key) !== text) throw new LiveError('fixture-conflict', 'FIXTURE · 原请求内容不匹配。', true);
    if (operation === 'sessions.send') {
      if (scenario === 'failure' || target.status !== 'online') throw new LiveError('fixture-rejected', 'FIXTURE · 消息明确未发送。', true, false);
      if (!accepted.has(key)) {
        originalText.set(key, text);
        accepted.set(key, { status: 'accepted', commandId: key, conversationId, messageId: `fixture-${key}` });
        events.push({ eventId: `fixture-${key}`, kind: 'user', text: request.text, at: eventTime(events.length) });
        if (scenario === 'success') events.push({ eventId: `fixture-reply-${key}`, kind: 'assistant', text: `FIXTURE · 已收到：${text}`, at: eventTime(events.length) });
        pending.set(key, { sessionId, targetUri: target.targetUri, request, status: 'unknown' });
      }
      if (scenario === 'unknown') throw new LiveError('transport', 'FIXTURE · 响应丢失，请查询原消息结果。');
      return accepted.get(key);
    }
    if (operation === 'sessions.send.lookup') return accepted.get(key) ?? { status: 'unknown' };
    if (operation === 'sessions.send.confirm' && accepted.has(key)) { pending.delete(key); return accepted.get(key); }
    throw new LiveError('unsupported', 'FIXTURE · 不支持此操作。', true);
  } };
}
