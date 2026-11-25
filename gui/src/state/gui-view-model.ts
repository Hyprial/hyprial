/**
 * Presentation-only view models for the fixture-mode GUI slice.
 *
 * These types describe what the shared pages render; they are not a wire
 * protocol and do not define operations, authentication, transport or
 * permission semantics. This batch ships only a placeholder adapter and an
 * explicit HTTP 501 response. The later contract batch owns the formal
 * business contract; no live adapter exists here.
 */

export type GuiPage =
  | 'start'
  | 'chat'
  | 'contacts'
  | 'org'
  | 'device'
  | 'connections'
  | 'settings'
  | 'first-run';

export type TargetAvailability =
  | 'available'
  | 'needs-connection'
  | 'disabled';

export interface SessionTarget {
  readonly id: string;
  readonly agentId?: string | null;
  readonly kind: 'agent' | 'human' | 'group';
  readonly displayName: string;
  readonly role: string;
  readonly availability: TargetAvailability;
  readonly avatarTone: 'green' | 'lilac' | 'peach' | 'grey';
  readonly modelServiceId?: string;
  readonly capabilities: readonly string[];
}

/**
 * Neutral agent view model: a session target plus the model-service label the
 * shared chat/agent views render. Product state carries this view; fixture
 * instances live under `dev/fixtures/`.
 */
export interface SessionAgent extends SessionTarget {
  readonly modelLabel: string;
}

/**
 * Presentation state for a message delivery.
 *
 * The adapter owns the real operation and the decision to retry; the UI must
 * not repeat an ambiguous write. `unknown` means the original request must be
 * checked before any retry, not that the write failed.
 */
export type MessageDeliveryState =
  | 'not-sent'
  | 'pending'
  | 'sent'
  | 'failed'
  | 'unknown';

export interface MessageReplyReference {
  readonly messageId: string;
  readonly authorName: string;
  readonly excerpt: string;
}

export interface ConversationMessage {
  readonly id: string;
  readonly targetId: string;
  readonly author: 'user' | 'assistant' | 'system';
  readonly text: string;
  readonly createdAt?: string;
  /** Absent for seeded fixture history; local sends use `not-sent`. */
  readonly delivery?: MessageDeliveryState;
  /** The conversation holds this message without its body; `text` is empty. */
  readonly bodyNotRecorded?: boolean;
  readonly authorId?: string;
  readonly authorName?: string;
  readonly replyTo?: MessageReplyReference;
  readonly mentions?: readonly string[];
  readonly membershipEvent?: boolean;
}

export interface ConnectionSummary {
  readonly id: string;
  readonly kind: 'model' | 'adapter';
  readonly label: string;
  readonly detail: string;
  readonly status: 'connected' | 'unconfigured' | 'disabled';
  readonly usedBy: number;
  readonly isDefault?: boolean;
}
