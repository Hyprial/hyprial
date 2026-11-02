import type { FixtureAgent } from '../../fixtures/demo';

/**
 * Product copy for the direct-chat journey.
 *
 * The three unavailable reasons stay separate: a missing model connection, a
 * disabled Agent, and a build without a send interface are different facts and
 * must not be reported with one shared sentence.
 */
export const CHAT_NEEDS_CONNECTION = '需要连接模型服务';
export const CHAT_NEEDS_ENABLE = '需要启用 Agent';
export const CHAT_INTERFACE_UNAVAILABLE = '消息功能暂不可用';
export const CHAT_SEND_FAILED = '消息未发送成功。内容已保留，请重试。';
export const CHAT_COMPOSER_READY = '发送后会显示处理结果。';
export const CHAT_EMPTY = '从第一条消息开始。';
export const CHAT_SENDING = '发送中…';

/** Delay before a local send resolves; keeps the pending state observable. */
export const CHAT_SEND_DELAY_MS = 300;

/** Why an Agent cannot take a message, or null when it can. */
export function getChatUnavailableNotice(
  target: FixtureAgent,
): string | null {
  if (target.availability === 'needs-connection') {
    return CHAT_NEEDS_CONNECTION;
  }
  if (target.availability === 'disabled') {
    return CHAT_NEEDS_ENABLE;
  }
  return null;
}
