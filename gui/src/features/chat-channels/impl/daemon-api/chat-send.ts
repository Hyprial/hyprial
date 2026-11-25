/**
 * Decision record (chat C1 single-target send + refresh recovery, 2026-11-05)
- #2426 merged the daemon-side `message.send` / `message.send.key_lookup` contract
  and the #2423 design. The public MethodSpec registry, OpenAPI, generated client
  and catalog pin are still absent, so this adapter is typed against the frozen
  contract shape and is only reachable through explicit injection. Ordinary
  product chat keeps sending unavailable until the public declaration lands.
- The operation key is the only recovery fact a tab keeps: 32 CSPRNG bytes as a
  padding-free base64url string in sessionStorage, written and read back verbatim
  before any dispatch. It is never placed in a URL, log, analytics payload or
  localStorage, and no target, conversation, body, summary, token or principal is
  stored. A tab keeps at most one unsettled key.
- One dispatch per intent. A lost response, an unmatched acceptance shape or an
  `unknown` lookup never triggers a resend. A key is cleared only by a matched
  durable acceptance, by the lookup's matched `accepted` record, or by explicit
  cancel/sign-out/identity change.
- Accepted means the daemon durably accepted the operation key. It is not proof
  that the peer received anything, nor that the original text still matches.
- `dispatch: not-dispatched` from the transport clears the key on failure, and
  so does a `dispatched` refusal whose code is one of the declared admission
  refusals in DEFINITIVE_SEND_REFUSALS (UUID addressing, 2026-11-06): the daemon
  answered before durable acceptance, so the fresh key has no record to find.
  Every other failure stays unknown with the key retained.
- A refusal before dispatch (catalog access or a transport refusal classified by
  methodRefusal) is `not-started` / `method-refused` with machine tokens only
  (`code`, `source`); it carries no notice text and the caller raises the
  notification (PR 1f/F3, 2026-11-14).
- A send may omit `conversationId` (slice G1, 2026-11-18): the daemon files it
  under the default conversation of the user and the target and returns that
  id at top level and on the delivery. The params then carry no
  `conversationId` key at all, and acceptance takes the returned id when it is
  a non-empty, whitespace-free string that the delivery, if it names one,
  repeats. A send that names an id still requires the daemon to echo it. The
  accepted outcome always reports the id the daemon stored, so the caller
  adopts it instead of inventing one; key lookup already returns the stored id.
 */

import { ClientError, DomainError, accessRefusal, methodRefusal, type MethodAccess } from '../../../../client';

export const CHAT_SEND_OPERATION_KEY_NAME = 'hyprial.gui.chat.send.operationKey';
export const CHAT_SEND_ACCEPTED_NOTICE = '消息已提交。';
export const CHAT_SEND_UNKNOWN_NOTICE = '发送结果未知；不会自动重发，可用该操作键手动查询。';
export const CHAT_SEND_STORE_FAILED_NOTICE = '无法保存恢复键，发送未开始。';
export const CHAT_SEND_PENDING_NOTICE = '本标签页还有一个未结算的发送操作键；请先查询或放弃该操作键。';
export const CHAT_SEND_INVALID_TARGET_NOTICE = '只能向一个 canonical agent 目标发送；本次未派发。';
export const CHAT_SEND_INVALID_INTENT_NOTICE = '发送内容或会话不完整；本次未派发。';
export const CHAT_SEND_RECOVERY_ACCEPTED_NOTICE = '该操作键已有持久发送记录；不表示对方收到，也不能证明原文与丢失输入完全一致。';
export const CHAT_SEND_RECOVERY_UNKNOWN_NOTICE = '无法确认该操作是否已持久接受；不会自动重发。';
export const CHAT_SEND_NO_RECOVERY_NOTICE = '没有可恢复的发送操作键。';
export const CHAT_SEND_REFUSED_NOTICE = 'daemon 明确拒绝了本次发送，消息未被接受。';

/**
 * Declared `message.send` admission refusals: the daemon answers them before
 * it durably accepts the operation key, so a `dispatched` refusal with one of
 * these codes is a definitive non-acceptance. Delivery-time codes
 * (USER_DELIVERY_*, ROUTE_*, SUBMIT_OUTCOME_UNKNOWN, ...) are not listed.
 */
const DEFINITIVE_SEND_REFUSALS: Readonly<Record<string, string>> = {
  AGENT_ADDRESS_RETIRED: '该 Agent 的名字地址已停用，本次未发送。',
  TARGET_UNRESOLVED: '找不到这个 Agent（可能已删除），本次未发送。',
  SESSION_SUPERSEDED: '当前会话已被新的会话取代，本次未发送；请刷新后重试。',
  AMBIGUOUS_TARGET: '目标对应多个 Agent，本次未发送。',
  INVALID_AGENT_NAME: '目标 Agent 名称无效，本次未发送。',
  TARGET_IS_NODE: '目标是节点而不是 Agent，本次未发送。',
  UNSUPPORTED_TARGET: '不支持向该目标发送，本次未发送。',
  TARGET_NOT_AUTHORIZED: '没有向该 Agent 发送的权限，本次未发送。',
  CALLER_NOT_AUTHORIZED: '当前身份无权发送，本次未发送。',
  SENDER_UNRESOLVED: '无法确认发送者身份，本次未发送。',
  SENDER_NOT_AUTHENTICATED: '发送者身份未通过验证，本次未发送。',
  STALE_SESSION: '会话已过期，本次未发送；请刷新后重试。',
  INVALID_SESSION_SOURCE: '会话来源无效，本次未发送。',
  SUMMARY_REQUIRED: '缺少消息摘要，本次未发送。',
  INVALID_ARGUMENT: '发送参数无效，本次未发送。',
};

const OPERATION_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
/** The daemon's own attempt key (`frs-…`); it is durable in the attempt record. */
const SERVER_OPERATION_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/;
const UNKNOWN_REASONS = ['not-found', 'invalid-record', 'unavailable'] as const;

export interface ChatSendStorage {
  getItem(name: string): string | null;
  setItem(name: string, value: string): void;
  removeItem(name: string): void;
}

export interface ChatSendParams {
  readonly to: readonly string[];
  readonly message: string;
  readonly summary: string;
  readonly conversationId?: string;
  readonly idempotencyKey: string;
}

/** Frozen contract v1: the generated `message.send` / `message.send.key_lookup`. */
export interface ChatSendPort {
  send(params: ChatSendParams): Promise<unknown>;
  keyLookup(params: { readonly operationKey: string }): Promise<unknown>;
}

export interface ChatSendIntent {
  readonly to: readonly string[];
  readonly message: string;
  readonly summary?: string;
  /** Omitted for a first message: the daemon names the default conversation. */
  readonly conversationId?: string;
}

export type ChatSendRefusal =
  | 'store-failed'
  | 'pending-operation'
  | 'invalid-intent'
  | 'target-not-canonical'
  | 'capability-unavailable';

export interface ChatSendMethodRefused {
  readonly status: 'not-started';
  readonly reason: 'method-refused';
  readonly code: string;
  readonly source: string;
}

export type ChatSendOutcome =
  | { readonly status: 'accepted'; readonly operationKey: string; readonly conversationId: string; readonly messageId: string; readonly notice: string }
  | { readonly status: 'unknown'; readonly operationKey: string; readonly reason: 'dispatch-unknown' | 'response-mismatch'; readonly notice: string }
  | { readonly status: 'refused'; readonly operationKey: string; readonly reason: 'dispatch-refused'; readonly code: string; readonly notice: string }
  | { readonly status: 'not-started'; readonly reason: ChatSendRefusal; readonly notice: string }
  | ChatSendMethodRefused;

export function isChatSendMethodRefused(outcome: ChatSendOutcome): outcome is ChatSendMethodRefused {
  return outcome.status === 'not-started' && outcome.reason === 'method-refused';
}

export type ChatSendRecovery =
  | { readonly status: 'idle'; readonly notice: string }
  | { readonly status: 'accepted'; readonly operationKey: string; readonly conversationId: string; readonly messageId: string; readonly notice: string }
  | { readonly status: 'unknown'; readonly operationKey: string; readonly reason: (typeof UNKNOWN_REASONS)[number] | 'response-mismatch'; readonly notice: string };

export interface ChatSendApi {
  readonly storageName: string;
  readonly pendingOperationKey: () => string | null;
  send(intent: ChatSendIntent): Promise<ChatSendOutcome>;
  recover(): Promise<ChatSendRecovery>;
  /**
   * P3 first-run: the daemon issued the durable key and the attempt is the
   * recovery record, so this path owns no tab storage.  The same one-dispatch
   * rule applies; a lost response is reconciled by `recoverKey`.
   */
  sendWithKey(intent: ChatSendIntent, operationKey: string): Promise<ChatSendOutcome>;
  recoverKey(operationKey: string): Promise<ChatSendRecovery>;
  clear(reason: 'cancelled' | 'identity-change' | 'sign-out'): void;
}

export interface ChatSendOptions {
  readonly access?: MethodAccess;
  readonly storage?: ChatSendStorage;
  readonly randomBytes?: (count: number) => Uint8Array;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function conversationIdOf(value: unknown): value is string {
  return nonEmpty(value) && !/\s/.test(value);
}

function refusalNotice(code: string, data: unknown): string {
  const base = DEFINITIVE_SEND_REFUSALS[code] ?? CHAT_SEND_REFUSED_NOTICE;
  const address = isObject(data) && nonEmpty(data.address) ? data.address : null;
  return address ? `${base}请改用新地址：${address}` : base;
}

/** Mirrors the declared agent URI shape: three non-empty, colon-free segments. */
export function isCanonicalAgentUri(uri: unknown): uri is string {
  if (typeof uri !== 'string') return false;
  const parts = uri.split(':');
  if (parts.length !== 4 || parts[0] !== 'agent') return false;
  return parts.slice(1).every(segment => /^(?:[^:\s]|[^:\s][^:]*[^:\s])$/.test(segment));
}

function newOperationKey(randomBytes: (count: number) => Uint8Array): string {
  const bytes = randomBytes(32);
  if (!(bytes instanceof Uint8Array) || bytes.length !== 32) throw new Error('invalid random source');
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const key = btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  if (!OPERATION_KEY_PATTERN.test(key)) throw new Error('invalid operation key');
  return key;
}

function defaultStorage(): ChatSendStorage {
  return globalThis.sessionStorage;
}

function summaryOf(message: string): string {
  const firstLine = message.trim().split('\n')[0].trim();
  return firstLine.slice(0, 80);
}

export function createChatSendApi(port: ChatSendPort, options: ChatSendOptions = {}): ChatSendApi {
  const storage = options.storage ?? defaultStorage();
  const randomBytes = options.randomBytes
    ?? ((count: number) => globalThis.crypto.getRandomValues(new Uint8Array(count)));

  function pendingOperationKey(): string | null {
    try {
      const stored = storage.getItem(CHAT_SEND_OPERATION_KEY_NAME);
      return stored === null ? null : stored;
    } catch {
      return null;
    }
  }

  function clearStorage(): void {
    try {
      storage.removeItem(CHAT_SEND_OPERATION_KEY_NAME);
    } catch {
      return;
    }
  }

  function clearStorageIfCurrent(operationKey: string): void {
    try {
      if (storage.getItem(CHAT_SEND_OPERATION_KEY_NAME) === operationKey) {
        storage.removeItem(CHAT_SEND_OPERATION_KEY_NAME);
      }
    } catch {
      return;
    }
  }

  function persist(operationKey: string): boolean {
    try {
      storage.setItem(CHAT_SEND_OPERATION_KEY_NAME, operationKey);
      return storage.getItem(CHAT_SEND_OPERATION_KEY_NAME) === operationKey;
    } catch {
      return false;
    }
  }

  function normalise(intent: ChatSendIntent): ChatSendParams | null {
    if (!Array.isArray(intent.to) || intent.to.length !== 1) return null;
    if (!isCanonicalAgentUri(intent.to[0])) return null;
    const message = typeof intent.message === 'string' ? intent.message : '';
    if (message.trim().length === 0) return null;
    if (intent.conversationId !== undefined && !conversationIdOf(intent.conversationId)) return null;
    const summary = (intent.summary ?? summaryOf(message)).trim();
    if (summary.length === 0) return null;
    return {
      to: [intent.to[0]], message, summary,
      ...(intent.conversationId === undefined ? {} : { conversationId: intent.conversationId }),
      idempotencyKey: '',
    };
  }

  function acceptedSend(
    raw: unknown, operationKey: string, target: string, conversationId: string | undefined,
  ): { readonly messageId: string; readonly conversationId: string } | null {
    if (!isObject(raw) || raw.ok !== true || raw.operationId !== operationKey) return null;
    const stored = raw.conversationId;
    if (!conversationIdOf(stored) || (conversationId !== undefined && stored !== conversationId)) return null;
    if (!nonEmpty(raw.messageId)) return null;
    const deliveries = raw.deliveries;
    if (!Array.isArray(deliveries) || deliveries.length !== 1) return null;
    const delivery = deliveries[0];
    if (!isObject(delivery) || delivery.target !== target || delivery.messageId !== raw.messageId) return null;
    if (delivery.conversationId !== undefined && delivery.conversationId !== stored) return null;
    return { messageId: raw.messageId, conversationId: stored };
  }

  function refusal(reason: ChatSendRefusal, notice: string): ChatSendOutcome {
    return { status: 'not-started', reason, notice };
  }

  function methodRefused(code: string, source: string): ChatSendMethodRefused {
    return { status: 'not-started', reason: 'method-refused', code, source };
  }

  return {
    storageName: CHAT_SEND_OPERATION_KEY_NAME,
    pendingOperationKey,
    clear() {
      clearStorage();
    },
    async send(intent) {
      if (pendingOperationKey() !== null) {
        return refusal('pending-operation', CHAT_SEND_PENDING_NOTICE);
      }
      const params = normalise(intent);
      if (params === null) {
        const targetValid = Array.isArray(intent.to) && intent.to.length === 1 && isCanonicalAgentUri(intent.to[0]);
        return targetValid
          ? refusal('invalid-intent', CHAT_SEND_INVALID_INTENT_NOTICE)
          : refusal('target-not-canonical', CHAT_SEND_INVALID_TARGET_NOTICE);
      }
      const refused = accessRefusal(options.access, 'message.send', params);
      if (refused !== null) return methodRefused(refused.code, 'message.send');
      let operationKey: string;
      try {
        operationKey = newOperationKey(randomBytes);
      } catch {
        return refusal('store-failed', CHAT_SEND_STORE_FAILED_NOTICE);
      }
      if (!persist(operationKey)) {
        clearStorage();
        return refusal('store-failed', CHAT_SEND_STORE_FAILED_NOTICE);
      }
      const target = params.to[0];
      try {
        const raw = await port.send({ ...params, idempotencyKey: operationKey });
        const accepted = acceptedSend(raw, operationKey, target, params.conversationId);
        if (accepted === null) {
          return { status: 'unknown', operationKey, reason: 'response-mismatch', notice: CHAT_SEND_UNKNOWN_NOTICE };
        }
        clearStorageIfCurrent(operationKey);
        return {
          status: 'accepted', operationKey, conversationId: accepted.conversationId,
          messageId: accepted.messageId, notice: CHAT_SEND_ACCEPTED_NOTICE,
        };
      } catch (error) {
        const transportRefusal = methodRefusal(error);
        if (transportRefusal !== null) {
          clearStorageIfCurrent(operationKey);
          return methodRefused(transportRefusal.code, transportRefusal.source ?? 'message.send');
        }
        if (error instanceof ClientError && error.dispatch === 'not-dispatched') {
          clearStorageIfCurrent(operationKey);
          return refusal('capability-unavailable', `发送未开始：${error.code}。`);
        }
        if (error instanceof ClientError && error.dispatch === 'dispatched'
          && Object.hasOwn(DEFINITIVE_SEND_REFUSALS, error.code)) {
          clearStorageIfCurrent(operationKey);
          const data = error instanceof DomainError ? error.data : undefined;
          return {
            status: 'refused', operationKey, reason: 'dispatch-refused', code: error.code,
            notice: refusalNotice(error.code, data),
          };
        }
        return { status: 'unknown', operationKey, reason: 'dispatch-unknown', notice: CHAT_SEND_UNKNOWN_NOTICE };
      }
    },
    async sendWithKey(intent, operationKey) {
      if (!SERVER_OPERATION_KEY.test(operationKey)) {
        return refusal('invalid-intent', CHAT_SEND_INVALID_INTENT_NOTICE);
      }
      const params = normalise(intent);
      if (params === null) {
        const targetValid = Array.isArray(intent.to) && intent.to.length === 1 && isCanonicalAgentUri(intent.to[0]);
        return targetValid
          ? refusal('invalid-intent', CHAT_SEND_INVALID_INTENT_NOTICE)
          : refusal('target-not-canonical', CHAT_SEND_INVALID_TARGET_NOTICE);
      }
      const refused = accessRefusal(options.access, 'message.send', params);
      if (refused !== null) return methodRefused(refused.code, 'message.send');
      const target = params.to[0];
      try {
        const raw = await port.send({ ...params, idempotencyKey: operationKey });
        const accepted = acceptedSend(raw, operationKey, target, params.conversationId);
        if (accepted === null) {
          return { status: 'unknown', operationKey, reason: 'response-mismatch', notice: CHAT_SEND_UNKNOWN_NOTICE };
        }
        return {
          status: 'accepted', operationKey, conversationId: accepted.conversationId,
          messageId: accepted.messageId, notice: CHAT_SEND_ACCEPTED_NOTICE,
        };
      } catch (error) {
        const transportRefusal = methodRefusal(error);
        if (transportRefusal !== null) {
          return methodRefused(transportRefusal.code, transportRefusal.source ?? 'message.send');
        }
        if (error instanceof ClientError && error.dispatch === 'not-dispatched') {
          return refusal('capability-unavailable', `发送未开始：${error.code}。`);
        }
        if (error instanceof ClientError && error.dispatch === 'dispatched'
          && Object.hasOwn(DEFINITIVE_SEND_REFUSALS, error.code)) {
          const data = error instanceof DomainError ? error.data : undefined;
          return {
            status: 'refused', operationKey, reason: 'dispatch-refused', code: error.code,
            notice: refusalNotice(error.code, data),
          };
        }
        return { status: 'unknown', operationKey, reason: 'dispatch-unknown', notice: CHAT_SEND_UNKNOWN_NOTICE };
      }
    },
    async recoverKey(operationKey) {
      if (!SERVER_OPERATION_KEY.test(operationKey)) {
        return { status: 'unknown', operationKey, reason: 'invalid-record', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      if (accessRefusal(options.access, 'message.send.key_lookup', { operationKey }) !== null) {
        return { status: 'unknown', operationKey, reason: 'unavailable', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      let raw: unknown;
      try {
        raw = await port.keyLookup({ operationKey });
      } catch {
        return { status: 'unknown', operationKey, reason: 'unavailable', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      if (isObject(raw) && raw.status === 'accepted' && raw.commandId === operationKey
        && nonEmpty(raw.conversationId) && nonEmpty(raw.messageId)) {
        return {
          status: 'accepted', operationKey, conversationId: raw.conversationId,
          messageId: raw.messageId, notice: CHAT_SEND_RECOVERY_ACCEPTED_NOTICE,
        };
      }
      const reason = isObject(raw) && raw.status === 'unknown'
        ? UNKNOWN_REASONS.find(value => value === raw.reason)
        : undefined;
      return {
        status: 'unknown', operationKey,
        reason: reason ?? 'response-mismatch', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE,
      };
    },
    async recover() {
      const operationKey = pendingOperationKey();
      if (operationKey === null) return { status: 'idle', notice: CHAT_SEND_NO_RECOVERY_NOTICE };
      if (!OPERATION_KEY_PATTERN.test(operationKey)) {
        return { status: 'unknown', operationKey, reason: 'invalid-record', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      if (accessRefusal(options.access, 'message.send.key_lookup', { operationKey }) !== null) {
        return { status: 'unknown', operationKey, reason: 'unavailable', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      let raw: unknown;
      try {
        raw = await port.keyLookup({ operationKey });
      } catch {
        return { status: 'unknown', operationKey, reason: 'unavailable', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE };
      }
      if (isObject(raw) && raw.status === 'accepted' && raw.commandId === operationKey
        && nonEmpty(raw.conversationId) && nonEmpty(raw.messageId)) {
        clearStorageIfCurrent(operationKey);
        return {
          status: 'accepted', operationKey, conversationId: raw.conversationId,
          messageId: raw.messageId, notice: CHAT_SEND_RECOVERY_ACCEPTED_NOTICE,
        };
      }
      const reason = isObject(raw) && raw.status === 'unknown'
        ? UNKNOWN_REASONS.find(value => value === raw.reason)
        : undefined;
      return {
        status: 'unknown', operationKey,
        reason: reason ?? 'response-mismatch', notice: CHAT_SEND_RECOVERY_UNKNOWN_NOTICE,
      };
    },
  };
}
