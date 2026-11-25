/** Frontend integration types, not a daemon/HTTP or authorization contract. */
export interface AgentReference {
  /** An ID returned by the agent capability, never inferred from displayName. */
  readonly targetId: string;
  readonly displayName: string;
  readonly lifecycle: 'unknown' | 'stopped' | 'starting' | 'running';
}

export interface ChatDraftHandoff {
  readonly targetId: string;
  readonly draft: string;
}

/** Created only after the chat capability reports a real request message ID. */
export interface FirstConversationAttempt {
  readonly targetId: string;
  readonly conversationId: string;
  readonly requestMessageId: string;
}

/** Supplied by the shared chat adapter, never accepted directly from a URL. */
export interface ReplyObservation {
  readonly source: 'live-adapter' | 'fixture' | 'preview';
  readonly targetId: string;
  readonly conversationId: string;
  readonly replyToMessageId: string | null;
  readonly messageId: string;
  readonly author: 'agent' | 'user' | 'system';
  readonly kind: 'message' | 'ack';
  readonly delivery: 'received' | 'pending' | 'failed';
  readonly text: string;
}

/** UI completion predicate; not authentication or proof of backend authority. */
export function confirmsFirstReply(attempt: FirstConversationAttempt, reply: ReplyObservation): boolean {
  return Boolean(attempt.targetId.trim() && attempt.conversationId.trim() && attempt.requestMessageId.trim()
    && reply.source === 'live-adapter' && reply.author === 'agent' && reply.kind === 'message'
    && reply.delivery === 'received' && reply.messageId.trim() && reply.text.trim()
    && reply.messageId !== attempt.requestMessageId
    && reply.targetId === attempt.targetId && reply.conversationId === attempt.conversationId
    && reply.replyToMessageId === attempt.requestMessageId);
}
