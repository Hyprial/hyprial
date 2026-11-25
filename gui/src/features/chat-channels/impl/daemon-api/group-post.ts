/**
 * Decision record (group chat post adapter, slice GC5-2, 2026-11-21):
 * - Adapts `conversations.post` via `createGroupPostApiFromClient` with a named
 *   `post` method so `capabilities.tsx` adds no inline request arrows and each
 *   user send dispatches at most one request.
 * - Per owner decision D1-A, the 43-char base64url `idempotencyKey` stays in
 *   memory only and never touches `sessionStorage` or the 1:1 recovery slot.
 * - Acceptance requires `ok === true`, `operationId === idempotencyKey`, matching
 *   `conversationId`, non-empty `messageId`, and `deliveries[0].accepted === true`;
 *   any mismatch (including Contract Mock's fixed example `operationId`) is
 *   reported as `unknown` so the UI never claims unverified success.
 * - Refusal copy maps strictly by error code (`CONVERSATION_NOT_MEMBER`,
 *   `CONVERSATION_NOT_FOUND`, `SUMMARY_REQUIRED`, `INVALID_ARGUMENT`), never by
 *   method name.
 */

import { ClientError, accessRefusal, methodRefusal, type MethodAccess } from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';

export const GROUP_POST_ACCEPTED_NOTICE = '消息已提交。';
export const GROUP_POST_UNKNOWN_NOTICE = '发送结果未知；请在上方消息里确认是否已发出。';
export const GROUP_POST_INVALID_INTENT_NOTICE = '发送内容或群聊 ID 不完整；本次未派发。';
export const GROUP_POST_REFUSED_NOTICE = 'daemon 明确拒绝了本次发送，消息未被接受。';
export const GROUP_POST_KEY_FAILED_NOTICE = '无法生成发送操作键，发送未开始。';

const DEFINITIVE_GROUP_POST_REFUSALS: Readonly<Record<string, string>> = {
  CONVERSATION_NOT_MEMBER: '你已不在这个群里，本次未发送。',
  CONVERSATION_NOT_FOUND: '这个群已不存在，本次未发送。',
  SUMMARY_REQUIRED: '缺少消息摘要，本次未发送。',
  INVALID_ARGUMENT: '发送参数无效，本次未发送。',
};

const OPERATION_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export interface GroupPostIntent {
  readonly conversationId: string;
  readonly message: string;
  readonly summary?: string;
  readonly replyTo?: string;
  readonly mentions?: readonly string[];
}

export interface GroupPostMethodRefused {
  readonly status: 'not-started';
  readonly reason: 'method-refused';
  readonly code: string;
  readonly source: string;
}

export type GroupPostOutcome =
  | {
    readonly status: 'accepted';
    readonly operationKey: string;
    readonly conversationId: string;
    readonly messageId: string;
    readonly duplicate: boolean;
    readonly notice: string;
  }
  | {
    readonly status: 'unknown';
    readonly operationKey: string;
    readonly reason: 'dispatch-unknown' | 'response-mismatch';
    readonly notice: string;
  }
  | {
    readonly status: 'refused';
    readonly operationKey: string;
    readonly reason: 'dispatch-refused';
    readonly code: string;
    readonly notice: string;
  }
  | {
    readonly status: 'not-started';
    readonly reason: 'invalid-intent' | 'capability-unavailable' | 'key-generation-failed';
    readonly notice: string;
  }
  | GroupPostMethodRefused;

export function isGroupPostMethodRefused(outcome: GroupPostOutcome): outcome is GroupPostMethodRefused {
  return outcome.status === 'not-started' && outcome.reason === 'method-refused';
}

export interface GroupPostApi {
  post(intent: GroupPostIntent): Promise<GroupPostOutcome>;
}

export interface GroupPostOptions {
  readonly access?: MethodAccess;
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

function summaryOf(message: string): string {
  const firstLine = message.trim().split('\n')[0].trim();
  return firstLine.slice(0, 80);
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

interface NormalisedGroupPost {
  readonly conversationId: string;
  readonly message: string;
  readonly summary: string;
  readonly replyTo?: string;
  readonly mentions?: string[];
}

function normalise(intent: GroupPostIntent): NormalisedGroupPost | null {
  if (!conversationIdOf(intent.conversationId)) return null;
  const message = typeof intent.message === 'string' ? intent.message : '';
  if (message.trim().length === 0) return null;
  const summary = (intent.summary ?? summaryOf(message)).trim();
  if (summary.length === 0) return null;
  if (intent.replyTo !== undefined && !nonEmpty(intent.replyTo)) return null;
  if (intent.mentions !== undefined && (!Array.isArray(intent.mentions) || !intent.mentions.every(nonEmpty))) {
    return null;
  }
  const mentions = intent.mentions && intent.mentions.length > 0 ? [...intent.mentions] : undefined;
  return {
    conversationId: intent.conversationId,
    message,
    summary,
    ...(intent.replyTo === undefined ? {} : { replyTo: intent.replyTo }),
    ...(mentions === undefined ? {} : { mentions }),
  };
}

function acceptedPost(
  raw: unknown,
  operationKey: string,
  conversationId: string,
): { readonly messageId: string; readonly duplicate: boolean } | null {
  if (!isObject(raw) || raw.ok !== true || raw.operationId !== operationKey) return null;
  if (raw.conversationId !== conversationId || !nonEmpty(raw.messageId)) return null;
  const deliveries = raw.deliveries;
  if (!Array.isArray(deliveries) || deliveries.length !== 1) return null;
  const delivery = deliveries[0];
  if (!isObject(delivery) || delivery.accepted !== true) return null;
  if (delivery.target !== conversationId || delivery.messageId !== raw.messageId) return null;
  if (delivery.conversationId !== undefined && delivery.conversationId !== conversationId) return null;
  return { messageId: raw.messageId, duplicate: delivery.duplicate === true };
}

export function createGroupPostApiFromClient(
  client: Partial<Pick<GeneratedClient, 'conversations'>>,
  options: GroupPostOptions = {},
): GroupPostApi {
  const randomBytes = options.randomBytes
    ?? ((count: number) => globalThis.crypto.getRandomValues(new Uint8Array(count)));

  return {
    async post(intent) {
      const params = normalise(intent);
      if (params === null) {
        return { status: 'not-started', reason: 'invalid-intent', notice: GROUP_POST_INVALID_INTENT_NOTICE };
      }
      const refused = accessRefusal(options.access, 'conversations.post', params);
      if (refused !== null) {
        return { status: 'not-started', reason: 'method-refused', code: refused.code, source: 'conversations.post' };
      }
      if (!client.conversations?.post) {
        return {
          status: 'not-started',
          reason: 'method-refused',
          code: 'GUI_CAPABILITY_UNAVAILABLE',
          source: 'conversations.post',
        };
      }
      let operationKey: string;
      try {
        operationKey = newOperationKey(randomBytes);
      } catch {
        return { status: 'not-started', reason: 'key-generation-failed', notice: GROUP_POST_KEY_FAILED_NOTICE };
      }
      try {
        const raw = await client.conversations.post({ ...params, idempotencyKey: operationKey });
        const accepted = acceptedPost(raw, operationKey, params.conversationId);
        if (accepted === null) {
          return { status: 'unknown', operationKey, reason: 'response-mismatch', notice: GROUP_POST_UNKNOWN_NOTICE };
        }
        return {
          status: 'accepted',
          operationKey,
          conversationId: params.conversationId,
          messageId: accepted.messageId,
          duplicate: accepted.duplicate,
          notice: GROUP_POST_ACCEPTED_NOTICE,
        };
      } catch (error) {
        const transportRefusal = methodRefusal(error);
        if (transportRefusal !== null) {
          return {
            status: 'not-started',
            reason: 'method-refused',
            code: transportRefusal.code,
            source: transportRefusal.source ?? 'conversations.post',
          };
        }
        if (error instanceof ClientError && error.dispatch === 'not-dispatched') {
          return { status: 'not-started', reason: 'capability-unavailable', notice: `发送未开始：${error.code}。` };
        }
        if (error instanceof ClientError && error.dispatch === 'dispatched'
          && Object.hasOwn(DEFINITIVE_GROUP_POST_REFUSALS, error.code)) {
          return {
            status: 'refused',
            operationKey,
            reason: 'dispatch-refused',
            code: error.code,
            notice: DEFINITIVE_GROUP_POST_REFUSALS[error.code],
          };
        }
        return { status: 'unknown', operationKey, reason: 'dispatch-unknown', notice: GROUP_POST_UNKNOWN_NOTICE };
      }
    },
  };
}
