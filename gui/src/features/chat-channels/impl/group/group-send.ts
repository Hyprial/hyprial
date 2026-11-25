/**
 * Decision record (group chat send controller, slice GC5-2, 2026-11-21):
 * - Owns per-group draft text, reply target, send lifecycle (`in-flight` /
 *   `success` / `definitive-failure` / `unknown`), and optimistic local sent
 *   entries outside `connected-chat.ts`.
 * - Each `send()` call issues at most one `conversations.post` port call and
 *   never auto-resends on `unknown` or touches the 1:1 `sessionStorage` key.
 * - Local `delivery: 'sent'` messages render immediately after acceptance and
 *   are superseded by authoritative history entries with the same `messageId`
 *   via `mergeGroupDisplayedMessages` / `reconcileMessages`.
 * - Editing the draft clears `definitive-failure` / `not-sent` / `success` so
 *   the user can revise and resend, while `unknown` stays visible until the
 *   user explicitly dismisses it.
 * - Slice GC5-4 (2026-11-21): tracks per-group selected `mentions` (member ids),
 *   pre-selects an agent author when replying to an agent message (never on own
 *   messages or membership events), sends non-empty `mentions` in
 *   `conversations.post`, and clears selected `mentions` on acceptance.
 */

import { useMemo } from 'react';
import type { UnavailableDetail } from '../../../../components/notifications';
import type { ConversationMessage } from '../../../../state/gui-view-model';
import type { OperationStatus } from '../../../../state/operation-lifecycle';
import { isGroupPostMethodRefused, type GroupPostApi } from '../daemon-api/group-post';

export interface GroupReplyTarget {
  readonly messageId: string;
  readonly authorName: string;
  readonly excerpt: string;
}

export interface GroupSendPort {
  readonly post: GroupPostApi;
  readonly reportUnavailable?: (subject: string, detail: UnavailableDetail) => void;
  readonly identity?: string;
  readonly resolveMemberName?: (groupId: string, memberId: string) => string;
}

export interface GroupSendOperation {
  readonly targetId: string;
  readonly requestId: string;
  readonly status: OperationStatus;
  readonly canRetry: false;
  readonly error?: string;
}

export interface GroupSendSnapshot {
  readonly drafts: Readonly<Record<string, string>>;
  readonly replyTargets: Readonly<Record<string, GroupReplyTarget | undefined>>;
  readonly mentions: Readonly<Record<string, readonly string[]>>;
  readonly operations: Readonly<Record<string, GroupSendOperation | undefined>>;
  readonly sentMessages: Readonly<Record<string, readonly ConversationMessage[]>>;
}

export const EMPTY_GROUP_SEND_SNAPSHOT: GroupSendSnapshot = {
  drafts: {},
  replyTargets: {},
  mentions: {},
  operations: {},
  sentMessages: {},
};

function replyExcerptOf(text: string): string {
  return text.trim().replace(/\s+/g, ' ').slice(0, 120);
}

/** Merges authoritative group history with locally accepted sends by message id. */
export function mergeGroupDisplayedMessages(
  historyMessages: readonly ConversationMessage[],
  localSent: readonly ConversationMessage[] | undefined,
): readonly ConversationMessage[] {
  if (!localSent || localSent.length === 0) return historyMessages;
  const historyIds = new Set(historyMessages.map(message => message.id));
  const pending = localSent.filter(message => !historyIds.has(message.id));
  if (pending.length === 0) return historyMessages;
  return [...historyMessages, ...pending];
}

/** Owns group send state and reply selection; components stay controlled. */
export class GroupSendController {
  private snapshot: GroupSendSnapshot = EMPTY_GROUP_SEND_SNAPSHOT;
  private listeners = new Set<() => void>();

  constructor(private readonly port: GroupSendPort) {}

  getSnapshot = (): GroupSendSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(change: Partial<GroupSendSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...change };
    this.listeners.forEach(listener => listener());
  }

  setDraft = (groupId: string, value: string): void => {
    const currentOp = this.snapshot.operations[groupId];
    const clearsOperation = currentOp !== undefined
      && (currentOp.status === 'definitive-failure' || currentOp.status === 'not-sent' || currentOp.status === 'success');
    this.update({
      drafts: { ...this.snapshot.drafts, [groupId]: value },
      ...(clearsOperation ? { operations: { ...this.snapshot.operations, [groupId]: undefined } } : {}),
    });
  };

  setReplyTarget(groupId: string, message: ConversationMessage): void {
    if (!groupId || message.membershipEvent || message.author === 'system') return;
    const authorName = message.author === 'user' ? '你' : (message.authorName ?? message.targetId);
    const excerpt = message.bodyNotRecorded ? '正文未记录' : replyExcerptOf(message.text);
    const replyMentionId = message.author === 'assistant'
      && message.authorId
      && message.authorId !== this.port.identity
      && !message.authorId.startsWith('user:')
      ? message.authorId
      : undefined;
    const currentMentions = this.snapshot.mentions[groupId] ?? [];
    const nextMentions = replyMentionId && !currentMentions.includes(replyMentionId)
      ? [...currentMentions, replyMentionId]
      : currentMentions;
    this.update({
      replyTargets: {
        ...this.snapshot.replyTargets,
        [groupId]: { messageId: message.id, authorName, excerpt },
      },
      ...(nextMentions !== currentMentions
        ? { mentions: { ...this.snapshot.mentions, [groupId]: nextMentions } }
        : {}),
    });
  }

  toggleMention(groupId: string, memberId: string): void {
    if (!groupId || !memberId) return;
    const current = this.snapshot.mentions[groupId] ?? [];
    const next = current.includes(memberId)
      ? current.filter(id => id !== memberId)
      : [...current, memberId];
    this.update({
      mentions: { ...this.snapshot.mentions, [groupId]: next },
    });
  }

  removeMention(groupId: string, memberId: string): void {
    if (!groupId || !memberId) return;
    const current = this.snapshot.mentions[groupId] ?? [];
    if (!current.includes(memberId)) return;
    this.update({
      mentions: { ...this.snapshot.mentions, [groupId]: current.filter(id => id !== memberId) },
    });
  }

  pruneMentions(groupId: string, validMemberIds: ReadonlySet<string>): void {
    const current = this.snapshot.mentions[groupId] ?? [];
    if (current.length === 0) return;
    const next = current.filter(id => validMemberIds.has(id));
    if (next.length === current.length) return;
    this.update({
      mentions: { ...this.snapshot.mentions, [groupId]: next },
    });
  }

  clearReplyTarget(groupId: string): void {
    if (!this.snapshot.replyTargets[groupId]) return;
    this.update({
      replyTargets: { ...this.snapshot.replyTargets, [groupId]: undefined },
    });
  }

  dismissUnknown(groupId: string): void {
    if (this.snapshot.operations[groupId]?.status !== 'unknown') return;
    this.update({
      operations: { ...this.snapshot.operations, [groupId]: undefined },
    });
  }

  reconcileMessages(groupId: string, historyMessages: readonly ConversationMessage[]): void {
    const local = this.snapshot.sentMessages[groupId];
    if (!local || local.length === 0) return;
    const historyIds = new Set(historyMessages.map(message => message.id));
    const remaining = local.filter(message => !historyIds.has(message.id));
    if (remaining.length === local.length) return;
    this.update({
      sentMessages: { ...this.snapshot.sentMessages, [groupId]: remaining },
    });
  }

  async send(groupId: string, text: string): Promise<void> {
    if (!this.port.identity || !groupId || text.trim().length === 0) return;
    const current = this.snapshot.operations[groupId];
    if (current?.status === 'in-flight' || current?.status === 'unknown') return;
    const replyTarget = this.snapshot.replyTargets[groupId];
    const mentions = this.snapshot.mentions[groupId] ?? [];
    this.update({
      operations: {
        ...this.snapshot.operations,
        [groupId]: { targetId: groupId, requestId: '', status: 'in-flight', canRetry: false },
      },
    });
    const outcome = await this.port.post.post({
      conversationId: groupId,
      message: text,
      ...(replyTarget ? { replyTo: replyTarget.messageId } : {}),
      ...(mentions.length > 0 ? { mentions: [...mentions] } : {}),
    });
    if (outcome.status === 'accepted') {
      const mentionNames = mentions.map(id => (this.port.resolveMemberName
        ? this.port.resolveMemberName(groupId, id)
        : id));
      const localMessage: ConversationMessage = {
        id: outcome.messageId,
        targetId: groupId,
        author: 'user',
        text,
        createdAt: new Date().toISOString(),
        delivery: 'sent',
        ...(replyTarget ? {
          replyTo: {
            messageId: replyTarget.messageId,
            authorName: replyTarget.authorName,
            excerpt: replyTarget.excerpt,
          },
        } : {}),
        ...(mentionNames.length > 0 ? { mentions: mentionNames } : {}),
      };
      const existing = this.snapshot.sentMessages[groupId] ?? [];
      const nextSent = [...existing.filter(item => item.id !== outcome.messageId), localMessage];
      this.update({
        drafts: { ...this.snapshot.drafts, [groupId]: '' },
        replyTargets: { ...this.snapshot.replyTargets, [groupId]: undefined },
        mentions: { ...this.snapshot.mentions, [groupId]: [] },
        operations: {
          ...this.snapshot.operations,
          [groupId]: { targetId: groupId, requestId: outcome.operationKey, status: 'success', canRetry: false },
        },
        sentMessages: { ...this.snapshot.sentMessages, [groupId]: nextSent },
      });
      return;
    }
    if (isGroupPostMethodRefused(outcome)) {
      this.update({
        operations: {
          ...this.snapshot.operations,
          [groupId]: { targetId: groupId, requestId: '', status: 'not-sent', canRetry: false },
        },
      });
      this.port.reportUnavailable?.('发送消息', { code: outcome.code, source: outcome.source });
      return;
    }
    if (outcome.status === 'refused') {
      this.update({
        operations: {
          ...this.snapshot.operations,
          [groupId]: {
            targetId: groupId,
            requestId: outcome.operationKey,
            status: 'definitive-failure',
            canRetry: false,
            error: outcome.notice,
          },
        },
      });
      return;
    }
    if (outcome.status === 'unknown') {
      this.update({
        operations: {
          ...this.snapshot.operations,
          [groupId]: {
            targetId: groupId,
            requestId: outcome.operationKey,
            status: 'unknown',
            canRetry: false,
            error: outcome.notice,
          },
        },
      });
      return;
    }
    this.update({
      operations: {
        ...this.snapshot.operations,
        [groupId]: {
          targetId: groupId,
          requestId: '',
          status: outcome.reason === 'capability-unavailable' || outcome.reason === 'key-generation-failed'
            ? 'definitive-failure' : 'not-sent',
          canRetry: false,
          error: outcome.notice,
        },
      },
    });
  }
}

/** Builds one group send controller per injected capability generation. */
export function useGroupSend(port: GroupSendPort | undefined): GroupSendController | null {
  return useMemo(() => (port ? new GroupSendController(port) : null), [port]);
}
