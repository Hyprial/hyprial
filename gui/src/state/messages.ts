import type { ConversationMessage } from './gui-view-model';

/**
 * Merge messages by their authoritative id.
 *
 * A later entry with the same id replaces the earlier one, while the first
 * position is kept. This prevents a replayed event or a reconnect snapshot from
 * rendering the same message twice. Fixture sends use local ids; a future
 * adapter must replace them with the ids returned by the canonical contract.
 */
export function mergeMessagesById(
  messages: readonly ConversationMessage[],
): readonly ConversationMessage[] {
  const byId = new Map<string, ConversationMessage>();
  for (const message of messages) {
    byId.set(message.id, message);
  }
  return [...byId.values()];
}

export function upsertMessage(
  messages: readonly ConversationMessage[],
  message: ConversationMessage,
): readonly ConversationMessage[] {
  return mergeMessagesById([...messages, message]);
}

/**
 * Merge a reconnect snapshot with incremental events that arrived afterwards.
 *
 * Snapshot entries come first so their original positions are kept; later
 * events replace the content of the same authoritative id. Repeating an event
 * only replaces its content again and never adds another row.
 */
export function mergeReconnectSnapshot(
  snapshot: readonly ConversationMessage[],
  events: readonly ConversationMessage[],
): readonly ConversationMessage[] {
  return mergeMessagesById([...snapshot, ...events]);
}

/**
 * Replace one optimistic local placeholder with the canonical server message.
 *
 * The local id is only a lookup key for the placeholder. The canonical message
 * must carry an authoritative id; a `local-*` id is rejected instead of being
 * promoted into authoritative history. If the canonical id already exists, the
 * later content wins and the existing position is kept.
 *
 * This is a pure adapter-facing helper, not a live adapter: no real service is
 * connected in this slice, and fixture sends still use `local-*` placeholders.
 */
export function reconcileLocalMessage(
  messages: readonly ConversationMessage[],
  localId: string,
  authoritative: ConversationMessage,
): readonly ConversationMessage[] {
  if (authoritative.id === localId || authoritative.id.startsWith('local-')) {
    throw new Error(
      'authoritative message id must come from the canonical source',
    );
  }
  return upsertMessage(
    messages.filter((message) => message.id !== localId),
    authoritative,
  );
}
