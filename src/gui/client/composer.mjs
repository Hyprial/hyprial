import { randomUUID } from 'node:crypto';

const fail = (message, code) => Object.assign(new Error(message), { code });

// Draft editing and delivery acknowledgement are separate state. A pending
// snapshot owns its destination, payload, and idempotency key until ACK/retry.
export function createComposer({ adapter, sessionRef }) {
  if (!adapter?.sessions || typeof adapter.sessions.send !== 'function' ||
      typeof adapter.sessions.cancel !== 'function' || typeof sessionRef !== 'function') {
    throw new TypeError('Composer requires a bound session adapter and sessionRef getter');
  }
  let draft = '';
  let pending = null;
  let lastAck = null;
  const acknowledgedTargets = new Map();

  async function deliver(intent) {
    const ack = await adapter.sessions.send(intent.sessionId, {
      text: intent.text,
      idempotencyKey: intent.key,
    });
    const result = { commandId: ack.commandId, acceptedAt: ack.acceptedAt, deduplicated: ack.deduplicated };
    acknowledgedTargets.set(result.commandId, intent.sessionId);
    if (pending === intent) {
      lastAck = result;
      pending = null;
    }
    return { ...result };
  }

  return {
    get draft() { return draft; },
    get lastAck() { return lastAck ? { ...lastAck } : null; },
    get hasPendingIntent() { return pending !== null; },
    setDraft(text) {
      if (typeof text !== 'string') throw fail('文本必须为字符串', 'COMPOSER_INVALID');
      draft = text.slice(0, 8000);
    },
    submit() {
      if (!draft.trim()) return Promise.reject(fail('文本为空', 'COMPOSER_EMPTY'));
      if (pending) return Promise.reject(fail('存在待确认意图；请显式 retry 或放弃', 'COMPOSER_PENDING'));
      const target = sessionRef();
      if (typeof target !== 'string' || !target) return Promise.reject(fail('当前会话无效', 'COMPOSER_SESSION'));
      pending = Object.freeze({ sessionId: target, text: draft, key: `intent-${randomUUID()}` });
      return deliver(pending);
    },
    retry() {
      if (!pending) return Promise.reject(fail('没有待重试意图', 'COMPOSER_NO_PENDING'));
      return deliver(pending);
    },
    abandonPending() { pending = null; },
    cancel(commandId) {
      const target = acknowledgedTargets.get(commandId) || sessionRef();
      if (typeof target !== 'string' || !target) return Promise.reject(fail('当前会话无效', 'COMPOSER_SESSION'));
      return adapter.sessions.cancel(target, commandId);
    },
  };
}
