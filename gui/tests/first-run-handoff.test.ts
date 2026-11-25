import { describe, expect, it } from 'vitest';
import { appReducer } from '../src/state/appReducer';
import { createFixtureState } from '../src/dev/fixtures/fixture-state';
import { confirmsFirstReply, type FirstConversationAttempt, type ReplyObservation } from '../src/features/onboarding/impl/handoff';

describe('first-run cross-feature handoff', () => {
  it('opens an existing target with a draft without sending or changing history', () => {
    const before = createFixtureState();
    const after = appReducer(before, { type: 'open-chat-draft', targetId: before.targets[1].id, draft: '第一条消息' });
    expect(after.page).toBe('chat');
    expect(after.activeTargetId).toBe(before.targets[1].id);
    expect(after.drafts[after.activeTargetId]).toBe('第一条消息');
    expect(after.threads).toBe(before.threads);
    expect(after.targetChat[after.activeTargetId].focusIntent).toEqual({ kind: 'composer' });
  });
  it('does not invent a target or overwrite a different existing draft', () => {
    const before = createFixtureState();
    expect(appReducer(before, { type: 'open-chat-draft', targetId: 'missing', draft: 'hello' })).toBe(before);
    const withDraft = { ...before, drafts: { [before.activeTargetId]: '未提交的原草稿' } };
    expect(appReducer(withDraft, { type: 'open-chat-draft', targetId: before.activeTargetId, draft: '新草稿' })).toBe(withDraft);
  });
  const attempt: FirstConversationAttempt = { targetId: 'stable-agent-id', conversationId: 'conversation-1', requestMessageId: 'request-1' };
  const reply: ReplyObservation = { source: 'live-adapter', targetId: attempt.targetId, conversationId: attempt.conversationId,
    replyToMessageId: attempt.requestMessageId, messageId: 'reply-1', author: 'agent', kind: 'message', delivery: 'received', text: '你好' };
  it('accepts only a correlated received agent reply', () => {
    expect(confirmsFirstReply(attempt, reply)).toBe(true);
    for (const patch of [
      { source: 'fixture' }, { source: 'preview' }, { author: 'user' }, { kind: 'ack' },
      { delivery: 'pending' }, { conversationId: 'other' }, { targetId: 'other' },
      { replyToMessageId: 'old-request' }, { messageId: '' }, { text: '   ' },
    ]) expect(confirmsFirstReply(attempt, { ...reply, ...patch } as ReplyObservation)).toBe(false);
    expect(confirmsFirstReply({ ...attempt, requestMessageId: '' }, reply)).toBe(false);
  });
});
