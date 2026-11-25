/** Chat-channels owns these opt-in test facts. They carry no credential or live authority. */
import { CHAT_FIXTURE_TARGET } from './directory';

export const CHAT_SESSIONS = {
  primary: { targetUri: CHAT_FIXTURE_TARGET, sessionId: 'fixture-session', conversationId: 'fixture-conversation' },
  empty: { targetUri: CHAT_FIXTURE_TARGET, events: [], nextCursor: null },
  history: { targetUri: CHAT_FIXTURE_TARGET, events: [
    { eventId: 'fixture-history-0', kind: 'user', text: '请确认今天的演示安排。', at: '2026-10-31T00:00:00.000Z' },
    { eventId: 'fixture-history-1', kind: 'assistant', text: 'FIXTURE · 安排已确认：先检查连接，再开始对话。', at: '2026-10-31T00:00:01.000Z' },
  ], nextCursor: null },
} as const;
