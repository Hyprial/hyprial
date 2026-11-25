import type { SessionAgent } from '../../../state/gui-view-model';

/**
 * Product copy for the direct-chat journey.
 *
 * The two unavailable reasons stay separate: a missing model connection and a
 * disabled Agent are different facts and must not share one sentence.
 */
export const CHAT_NEEDS_CONNECTION = '需要连接模型服务';
export const CHAT_NEEDS_ENABLE = '需要启用 Agent';

/** Delay before a local send resolves; keeps the pending state observable. */
export const CHAT_SEND_DELAY_MS = 300;

/** Why an Agent cannot take a message, or null when it can. */
export function getChatUnavailableNotice(
  target: SessionAgent,
): string | null {
  if (target.availability === 'needs-connection') {
    return CHAT_NEEDS_CONNECTION;
  }
  if (target.availability === 'disabled') {
    return CHAT_NEEDS_ENABLE;
  }
  return null;
}
