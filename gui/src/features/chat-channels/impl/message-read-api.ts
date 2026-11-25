/**
 * Decision record (chat reads the conversation document, slice G1, 2026-11-18)
- `history` reads `conversations.history`, the conversation document, keyed by
  its `c.` id (`documentId`). It no longer calls `message.history`.
- A directory head names two ids: `documentId` (the head's `conversationId`, the
  document) and `conversationId` (the head's `legacyConversationId`, the id
  `message.send` takes). Keeping the send id under the old name means every
  send-side caller is unchanged; a history caller must name the document.
- History entries are validated against the published schema, not more: an
  entry's `conversationId` is the send id and need not equal the document id,
  `from`, `to` and the entry `conversationId` are plain strings in the schema
  (no minimum length), `message` may be empty, `bodyOmitted` (when present) is a
  non-empty reason string, and `messageClass`, `bodyOmitted` and
  `recordAvailable` pass through when present.
- Group chat read support (slice GC5-1, 2026-11-21): `conversations()` accepts
  optional `kinds` (`['direct', 'group']`) and separates `kind: 'group'` heads
  into `groups` without breaking direct-only callers (omitting `groups` when no
  group kind was requested and none were returned). `history()` validates and
  passes through optional `replyTo`, `mentions` and `system` fields on group
  entries. `createGroupChatReadApiFromClient` shares `readHistory` so
  `capabilities.tsx` adds no inline request arrows.
 */
import { MethodRefusedError, assertMethodAvailable, type MethodAccess } from '../../../client';
import { createGeneratedClient, type GeneratedClient } from '../../../client/generated';
import type { ConversationsHistoryResult, MessageQueryResult, MessageSendLookupParams } from '../../../client/generated/types';

export interface MessageReadRequest {
  request(method: string, params: Record<string, unknown>): Promise<unknown>;
}
export type LookupRequest = Pick<MessageSendLookupParams, 'target' | 'text' | 'conversationId' | 'idempotencyKey'>;
export type LookupResult =
  | { readonly status: 'accepted'; readonly commandId: string; readonly conversationId: string; readonly messageId: string }
  | { readonly status: 'unknown'; readonly reason: 'not-found' | 'invalid-record' | 'request-mismatch' | 'unavailable' };
export type HistoryPage = Pick<
  ConversationsHistoryResult,
  'entries' | 'nextCursor' | 'headCursor' | 'nextOlderCursor' | 'recordAvailable' | 'memberNames'
>;
export type HistoryMode = 'tail' | 'older' | 'after';
export type ConversationKind = 'direct' | 'group';
export interface ConversationHead {
  /** The conversation document (`c.` id); the only id `history` accepts. */
  readonly documentId: string;
  /** The id `message.send` takes for this conversation. */
  readonly conversationId: string;
  readonly peer: string;
  readonly lastMessageAtMs: number;
}
export interface GroupHead {
  readonly conversationId: string;
  readonly title: string;
  readonly memberCount: number;
  readonly lastMessageAtMs: number;
}
export type ConversationPage = {
  readonly entries: readonly ConversationHead[];
  readonly groups?: readonly GroupHead[];
  readonly nextCursor: string | null;
  readonly recordAvailable?: boolean;
};
export type QueryEntry = MessageQueryResult['entries'][number];
export interface MessageReadApi {
  lookup(request: LookupRequest): Promise<LookupResult>;
  history(documentId: string, cursor?: string, mode?: HistoryMode): Promise<HistoryPage>;
  conversations(cursor?: string, kinds?: readonly ConversationKind[]): Promise<ConversationPage>;
  query(view: 'inbox' | 'outbox'): Promise<readonly QueryEntry[]>;
}
export interface GroupChatReadApi {
  history(groupId: string, cursor?: string, mode?: HistoryMode): Promise<HistoryPage>;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('消息读取结果尚未确认');
  return value as Record<string, unknown>;
}
function nonempty(value: unknown): value is string { return typeof value === 'string' && value.length > 0; }
function isObject(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value); }
const UNKNOWN_REASONS = ['not-found', 'invalid-record', 'request-mismatch', 'unavailable'] as const;
const MESSAGE_CLASSES = ['interaction', 'system'];
const SYSTEM_EVENTS = ['created', 'member-added', 'member-removed', 'member-left'];
function optionalBoolean(value: unknown): boolean { return value === undefined || typeof value === 'boolean'; }

function validGroupEntryFields(entry: Record<string, unknown>): boolean {
  if (entry.replyTo !== undefined) {
    if (!isObject(entry.replyTo) || !nonempty(entry.replyTo.messageId)
      || !nonempty(entry.replyTo.from) || typeof entry.replyTo.excerpt !== 'string') {
      return false;
    }
  }
  if (entry.mentions !== undefined) {
    if (!Array.isArray(entry.mentions) || !entry.mentions.every(nonempty)) return false;
  }
  if (entry.system !== undefined) {
    if (!isObject(entry.system) || !SYSTEM_EVENTS.includes(entry.system.event as string)
      || !nonempty(entry.system.memberId)) {
      return false;
    }
  }
  return true;
}

async function readHistory(
  client: Partial<Pick<GeneratedClient, 'conversations'>>,
  access: MethodAccess | undefined,
  documentId: string,
  cursor?: string,
  mode?: HistoryMode,
): Promise<HistoryPage> {
  if (!nonempty(documentId)) throw new Error('会话 ID 缺失');
  if (!client.conversations) throw new MethodRefusedError('conversations.history', 'GUI_CAPABILITY_UNAVAILABLE');
  const params = { conversationId: documentId, ...(cursor === undefined ? {} : { cursor }), ...(mode ? { mode } : {}) };
  assertMethodAvailable(access, 'conversations.history', params);
  const result = object(await client.conversations.history(params));
  if (result.ok !== true || result.conversationId !== documentId || !Array.isArray(result.entries) ||
      !(result.nextCursor === null || nonempty(result.nextCursor)) || !optionalBoolean(result.recordAvailable)) {
    throw new Error('历史消息格式不正确');
  }
  for (const raw of result.entries) {
    const entry = object(raw);
    if (entry.kind !== 'message' || !nonempty(entry.messageId) || typeof entry.conversationId !== 'string' ||
        typeof entry.from !== 'string' || typeof entry.to !== 'string' || typeof entry.message !== 'string' ||
        typeof entry.createdAtMs !== 'number' || !Number.isFinite(entry.createdAtMs) || !isObject(entry.origin) ||
        (entry.messageClass !== undefined && !MESSAGE_CLASSES.includes(entry.messageClass as string)) ||
        (entry.bodyOmitted !== undefined && !nonempty(entry.bodyOmitted)) ||
        !validGroupEntryFields(entry)) {
      throw new Error('历史消息格式不正确');
    }
  }
  for (const key of ['headCursor', 'nextOlderCursor']) {
    if (result[key] !== undefined && result[key] !== null && !nonempty(result[key])) throw new Error('历史消息游标格式不正确');
  }
  if (result.memberNames !== undefined) {
    if (!isObject(result.memberNames)
      || !Object.values(result.memberNames).every(value => typeof value === 'string')) {
      throw new Error('历史消息成员名称表格式不正确');
    }
  }
  return { entries: result.entries as HistoryPage['entries'], nextCursor: result.nextCursor as string | null,
    ...(result.headCursor === undefined ? {} : { headCursor: result.headCursor as string | null }),
    ...(result.nextOlderCursor === undefined ? {} : { nextOlderCursor: result.nextOlderCursor as string | null }),
    ...(result.recordAvailable === undefined ? {} : { recordAvailable: result.recordAvailable as boolean }),
    ...(result.memberNames === undefined ? {} : { memberNames: result.memberNames as HistoryPage['memberNames'] }) };
}

export function createMessageReadApi(port: MessageReadRequest): MessageReadApi {
  const client = createGeneratedClient({ invoke: async <T>(method: string, params: unknown): Promise<T> =>
    port.request(method, params as Record<string, unknown>) as Promise<T> });
  return createMessageReadApiFromClient(client);
}

export function createGroupChatReadApiFromClient(
  client: Partial<Pick<GeneratedClient, 'conversations'>>,
  access?: MethodAccess,
): GroupChatReadApi {
  return {
    async history(groupId, cursor, mode) {
      return readHistory(client, access, groupId, cursor, mode);
    },
  };
}

export function createMessageReadApiFromClient(
  client: Pick<GeneratedClient, 'message'> & Partial<Pick<GeneratedClient, 'conversations'>>,
  access?: MethodAccess,
): MessageReadApi {
  function requireAvailable(method: string, params: unknown): void {
    assertMethodAvailable(access, method, params);
  }
  return {
    async lookup(request) {
      if (![request.target, request.text, request.conversationId, request.idempotencyKey].every(nonempty)) {
        throw new Error('消息查询参数不完整');
      }
      requireAvailable('message.send.lookup', request);
      const result = object(await client.message.send.lookup(request));
      if (result.status === 'accepted' && result.commandId === request.idempotencyKey &&
          result.conversationId === request.conversationId && nonempty(result.messageId)) {
        return { status: 'accepted', commandId: result.commandId, conversationId: result.conversationId, messageId: result.messageId };
      }
      if (result.status === 'unknown' && UNKNOWN_REASONS.some(reason => reason === result.reason)) {
        return { status: 'unknown', reason: result.reason as (typeof UNKNOWN_REASONS)[number] };
      }
      throw new Error('原消息结果尚未确认');
    },
    async history(documentId, cursor, mode) {
      return readHistory(client, access, documentId, cursor, mode);
    },
    async conversations(cursor, kinds) {
      if (!client.conversations) throw new MethodRefusedError('conversations.list', 'GUI_CAPABILITY_UNAVAILABLE');
      const params = {
        ...(cursor === undefined ? {} : { cursor }),
        ...(kinds && kinds.length > 0 ? { kinds: [...kinds] } : {}),
      };
      requireAvailable('conversations.list', params);
      const result = object(await client.conversations.list(params));
      if (result.ok !== true || !nonempty(result.participant) || !Array.isArray(result.conversations)
        || !(result.nextCursor === null || nonempty(result.nextCursor)) || !optionalBoolean(result.recordAvailable)) {
        throw new Error('会话目录格式不正确');
      }
      const entries: ConversationHead[] = [];
      const groups: GroupHead[] = [];
      for (const raw of result.conversations) {
        const entry = object(raw);
        if (entry.kind === 'group') {
          if (!nonempty(entry.conversationId) || !nonempty(entry.title)
            || typeof entry.memberCount !== 'number' || !Number.isInteger(entry.memberCount) || entry.memberCount < 1
            || typeof entry.lastMessageAtMs !== 'number' || !Number.isFinite(entry.lastMessageAtMs)) {
            throw new Error('会话目录格式不正确');
          }
          groups.push({
            conversationId: entry.conversationId,
            title: entry.title,
            memberCount: entry.memberCount,
            lastMessageAtMs: entry.lastMessageAtMs,
          });
          continue;
        }
        if (!nonempty(entry.conversationId) || !nonempty(entry.legacyConversationId) || !nonempty(entry.peer)
          || typeof entry.lastMessageAtMs !== 'number' || !Number.isFinite(entry.lastMessageAtMs)) {
          throw new Error('会话目录格式不正确');
        }
        entries.push({ documentId: entry.conversationId, conversationId: entry.legacyConversationId, peer: entry.peer,
          lastMessageAtMs: entry.lastMessageAtMs });
      }
      const includeGroups = Boolean(kinds?.includes('group')) || groups.length > 0;
      return {
        entries,
        ...(includeGroups ? { groups } : {}),
        nextCursor: result.nextCursor as string | null,
        ...(result.recordAvailable === undefined ? {} : { recordAvailable: result.recordAvailable as boolean }),
      };
    },
    async query(view) {
      const params = { view };
      requireAvailable('message.query', params);
      const result = object(await client.message.query(params));
      if (result.ok !== true || result.view !== view || !nonempty(result.actor) || !Array.isArray(result.entries)) {
        throw new Error('消息目录格式不正确');
      }
      for (const raw of result.entries) {
        const entry = object(raw);
        if (!nonempty(entry.messageId) || !nonempty(entry.conversationId)) throw new Error('消息目录格式不正确');
      }
      return result.entries as QueryEntry[];
    },
  };
}
