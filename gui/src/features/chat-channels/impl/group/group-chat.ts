/**
 * Decision record (group chat read controller, slice GC5-1, 2026-11-21):
 * - Owns group selection, read-only history, 2s incremental polling, older page
 *   loading, and member-name resolution outside `connected-chat.ts` so the 1:1
 *   controller stays below its 1000-line ceiling.
 * - One public action issues at most one port call; `deselect()` clears the
 *   active group and increments `historyEpoch` so in-flight responses are fenced.
 * - Member names follow the 3-tier fallback (`resolveGroupMemberName`): caller
 *   identity -> per-entry name table (future B1) -> cached member list -> chat
 *   target directory (`agentId` match) -> raw `memberId`.
 * - Membership events (`system.event` + `system.memberId`) are projected into
 *   concise Chinese lines rendered inline without folding.
 * - Slice GC5-4 (2026-11-21): exposes `authorId` on non-self messages and
 *   `resolveMemberName()` on `GroupChatController` so reply pre-selection and
 *   mention chips share the same 3-tier name lookup without extra requests.
 */
import { useEffect, useMemo } from 'react';
import { ClientError, methodRefusal } from '../../../../client';
import type { UnavailableDetail } from '../../../../components/notifications';
import type { ConversationMessage } from '../../../../state/gui-view-model';
import type { GroupChatReadApi, GroupHead, HistoryPage } from '../message-read-api';

export interface GroupMemberNameSource {
  readonly memberId: string;
  readonly displayName: string;
}

export interface GroupTargetNameSource {
  readonly agentId?: string | null;
  readonly displayName?: string | null;
}

export interface GroupChatPort {
  readonly read: GroupChatReadApi;
  readonly refreshDirectory?: () => Promise<void>;
  readonly reportUnavailable?: (subject: string, detail: UnavailableDetail) => void;
  readonly identity?: string;
}

export interface GroupChatEntry {
  readonly historyStatus: 'loading' | 'ready' | 'error' | 'unread';
  readonly historyError?: string;
  readonly stale?: boolean;
  readonly membershipStatus?: 'not-member' | 'not-found';
  readonly messages: readonly ConversationMessage[];
  readonly nextCursor?: string | null;
  readonly headCursor?: string | null;
  readonly loadingMore?: boolean;
}

export interface GroupChatSnapshot {
  readonly activeGroupId: string | null;
  readonly entries: Readonly<Record<string, GroupChatEntry>>;
}

export const EMPTY_GROUP_CHAT_SNAPSHOT: GroupChatSnapshot = {
  activeGroupId: null,
  entries: {},
};

const EMPTY_GROUP_ENTRY: GroupChatEntry = {
  historyStatus: 'loading',
  messages: [],
};

interface GroupContext {
  readonly groups: readonly GroupHead[];
  readonly targets: readonly GroupTargetNameSource[];
  readonly membersByGroup: Readonly<Record<string, readonly GroupMemberNameSource[]>>;
  readonly entryNamesByGroup: Readonly<Record<string, Readonly<Record<string, string>>>>;
}

function errorCodeOf(error: unknown): string | undefined {
  if (error instanceof ClientError) return error.code;
  if (error && typeof error === 'object') {
    const code = (error as { readonly code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return undefined;
}

function failureText(error: unknown): string {
  if (error instanceof ClientError) {
    const requestId = error.diagnostics.requestId;
    return requestId ? `${error.code}（请求 ${requestId}）` : error.code;
  }
  return error instanceof Error && error.message ? error.message : '读取失败';
}

/** Resolves a member id to a display label using the 3-tier fallback. */
export function resolveGroupMemberName(
  memberId: string,
  options: {
    readonly identity?: string;
    readonly members?: readonly GroupMemberNameSource[];
    readonly targets?: readonly GroupTargetNameSource[];
    readonly entryNames?: Readonly<Record<string, string>>;
  },
): string {
  if (options.identity && memberId === options.identity) return '你';
  const entryName = options.entryNames?.[memberId];
  if (typeof entryName === 'string' && entryName.trim().length > 0) return entryName;
  const member = options.members?.find(row => row.memberId === memberId);
  if (member && member.displayName.trim().length > 0) return member.displayName;
  const target = options.targets?.find(row => row.agentId && row.agentId === memberId);
  if (target && typeof target.displayName === 'string' && target.displayName.trim().length > 0) {
    return target.displayName;
  }
  return memberId;
}

function formatMembershipEvent(
  entry: HistoryPage['entries'][number],
  groupTitle: string | undefined,
  options: Parameters<typeof resolveGroupMemberName>[1],
): string {
  const system = entry.system;
  if (!system) return entry.message;
  const actor = resolveGroupMemberName(entry.from, options);
  const member = resolveGroupMemberName(system.memberId, options);
  if (system.event === 'created') {
    return groupTitle ? `${actor}创建了群聊「${groupTitle}」` : `${actor}创建了群聊`;
  }
  if (system.event === 'member-added') return `${actor}添加了 ${member}`;
  if (system.event === 'member-removed') return `${actor}移除了 ${member}`;
  if (system.event === 'member-left') return `${member} 退出了群聊`;
  return entry.message;
}

function authorOf(from: string, identity?: string): ConversationMessage['author'] {
  if (identity) return from === identity ? 'user' : 'assistant';
  return from.startsWith('user:') ? 'user' : 'assistant';
}

function groupMessageOf(
  entry: HistoryPage['entries'][number],
  groupId: string,
  context: GroupContext,
  identity?: string,
): ConversationMessage {
  const members = context.membersByGroup[groupId];
  const entryNames = context.entryNamesByGroup[groupId];
  const groupTitle = context.groups.find(group => group.conversationId === groupId)?.title;
  const nameOptions = { identity, members, targets: context.targets, entryNames };
  const isSystem = entry.messageClass === 'system';
  if (isSystem && entry.system) {
    return {
      id: entry.messageId,
      targetId: groupId,
      author: 'system',
      text: formatMembershipEvent(entry, groupTitle, nameOptions),
      createdAt: new Date(entry.createdAtMs).toISOString(),
      membershipEvent: true,
    };
  }
  const author = isSystem ? 'system' : authorOf(entry.from, identity);
  const authorId = author === 'assistant' ? entry.from : undefined;
  const authorName = author === 'assistant' ? resolveGroupMemberName(entry.from, nameOptions) : undefined;
  const replyTo = entry.replyTo
    ? {
      messageId: entry.replyTo.messageId,
      authorName: resolveGroupMemberName(entry.replyTo.from, nameOptions),
      excerpt: entry.replyTo.excerpt,
    }
    : undefined;
  const mentions = entry.mentions && entry.mentions.length > 0
    ? entry.mentions.map(id => resolveGroupMemberName(id, nameOptions))
    : undefined;
  return {
    id: entry.messageId,
    targetId: groupId,
    author,
    text: entry.message,
    createdAt: new Date(entry.createdAtMs).toISOString(),
    ...(entry.bodyOmitted === undefined ? {} : { bodyNotRecorded: true }),
    ...(authorId === undefined ? {} : { authorId }),
    ...(authorName === undefined ? {} : { authorName }),
    ...(replyTo === undefined ? {} : { replyTo }),
    ...(mentions === undefined ? {} : { mentions }),
  };
}

function mergeGroupMessages(
  current: readonly ConversationMessage[],
  entries: readonly HistoryPage['entries'][number][],
  groupId: string,
  context: GroupContext,
  identity?: string,
): ConversationMessage[] {
  const messages = new Map(current.map(message => [message.id, message]));
  for (const item of entries) messages.set(item.messageId, groupMessageOf(item, groupId, context, identity));
  return [...messages.values()].sort((a, b) => (a.createdAt ?? '').localeCompare(b.createdAt ?? '') || a.id.localeCompare(b.id));
}

/** Owns read-only group chat projection; components stay controlled. */
export class GroupChatController {
  private snapshot: GroupChatSnapshot = EMPTY_GROUP_CHAT_SNAPSHOT;
  private listeners = new Set<() => void>();
  private historyEpoch = 0;
  private refreshing = false;
  private context: GroupContext = { groups: [], targets: [], membersByGroup: {}, entryNamesByGroup: {} };
  private rawEntries = new Map<string, readonly HistoryPage['entries'][number][]>();

  constructor(private readonly port: GroupChatPort) {}

  getSnapshot = (): GroupChatSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  setContext(groups: readonly GroupHead[], targets: readonly GroupTargetNameSource[]): void {
    if (this.context.groups === groups && this.context.targets === targets) return;
    this.context = { ...this.context, groups, targets };
    this.reprojectAll();
  }

  setMembers(groupId: string, members: readonly GroupMemberNameSource[]): void {
    this.context = {
      ...this.context,
      membersByGroup: { ...this.context.membersByGroup, [groupId]: members },
    };
    this.reprojectGroup(groupId);
  }

  private reprojectAll(): void {
    const nextEntries: Record<string, GroupChatEntry> = {};
    let changed = false;
    for (const [groupId, entry] of Object.entries(this.snapshot.entries)) {
      const raw = this.rawEntries.get(groupId);
      if (!raw) {
        nextEntries[groupId] = entry;
        continue;
      }
      changed = true;
      nextEntries[groupId] = {
        ...entry,
        messages: mergeGroupMessages([], raw, groupId, this.context, this.port.identity),
      };
    }
    if (changed) this.update({ entries: nextEntries });
  }

  private reprojectGroup(groupId: string): void {
    const entry = this.snapshot.entries[groupId];
    const raw = this.rawEntries.get(groupId);
    if (!entry || !raw) return;
    this.entry(groupId, {
      messages: mergeGroupMessages([], raw, groupId, this.context, this.port.identity),
    });
  }

  private update(change: Partial<GroupChatSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...change };
    this.listeners.forEach(listener => listener());
  }

  private entry(groupId: string, change: Partial<GroupChatEntry>): void {
    this.update({
      entries: {
        ...this.snapshot.entries,
        [groupId]: { ...(this.snapshot.entries[groupId] ?? EMPTY_GROUP_ENTRY), ...change },
      },
    });
  }

  private storeRawEntries(groupId: string, entries: HistoryPage['entries']): void {
    const existing = new Map((this.rawEntries.get(groupId) ?? []).map(item => [item.messageId, item]));
    for (const item of entries) existing.set(item.messageId, item);
    this.rawEntries.set(groupId, [...existing.values()]);
  }

  private mergeEntryNames(
    groupId: string,
    memberNames?: Readonly<Record<string, string>>,
    replace = false,
  ): void {
    if (!replace && !memberNames) return;
    const nextNames = replace
      ? { ...(memberNames ?? {}) }
      : { ...(this.context.entryNamesByGroup[groupId] ?? {}), ...memberNames };
    this.context = {
      ...this.context,
      entryNamesByGroup: { ...this.context.entryNamesByGroup, [groupId]: nextNames },
    };
  }

  deselect(): void {
    this.historyEpoch += 1;
    if (this.snapshot.activeGroupId !== null) this.update({ activeGroupId: null });
  }

  async select(groupId: string): Promise<void> {
    if (!groupId) return;
    const epoch = ++this.historyEpoch;
    const prior = this.snapshot.entries[groupId];
    this.update({ activeGroupId: groupId });
    this.entry(groupId, {
      historyStatus: 'loading',
      historyError: undefined,
      stale: undefined,
      membershipStatus: undefined,
      messages: [],
      nextCursor: null,
      headCursor: null,
      loadingMore: false,
    });
    try {
      const page = await this.port.read.history(groupId, undefined, 'tail');
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      this.rawEntries.delete(groupId);
      this.storeRawEntries(groupId, page.entries);
      this.mergeEntryNames(groupId, page.memberNames, true);
      this.entry(groupId, {
        historyStatus: 'ready',
        historyError: undefined,
        stale: undefined,
        membershipStatus: undefined,
        nextCursor: page.nextOlderCursor ?? page.nextCursor,
        headCursor: page.headCursor,
        messages: mergeGroupMessages([], page.entries, groupId, this.context, this.port.identity),
      });
    } catch (error) {
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        this.entry(groupId, prior && prior.historyStatus !== 'loading'
          ? { ...prior, loadingMore: false }
          : { historyStatus: 'unread', historyError: undefined, messages: [], nextCursor: null, loadingMore: false });
        this.port.reportUnavailable?.('读取消息', refusal);
        return;
      }
      const code = errorCodeOf(error);
      if (code === 'CONVERSATION_NOT_MEMBER') {
        this.entry(groupId, {
          historyStatus: 'error',
          membershipStatus: 'not-member',
          historyError: '你已不在这个群里。',
          messages: [],
        });
        return;
      }
      if (code === 'CONVERSATION_NOT_FOUND') {
        this.entry(groupId, {
          historyStatus: 'error',
          membershipStatus: 'not-found',
          historyError: '这个群已不存在。',
          messages: [],
        });
        return;
      }
      if (code === 'DAEMON_UNAVAILABLE' && prior && prior.messages.length > 0) {
        this.entry(groupId, {
          historyStatus: 'error',
          stale: true,
          historyError: `历史消息读取失败：${failureText(error)}`,
          messages: prior.messages,
          nextCursor: prior.nextCursor,
          headCursor: prior.headCursor,
        });
        return;
      }
      this.entry(groupId, {
        historyStatus: 'error',
        historyError: `历史消息读取失败：${failureText(error)}`,
        messages: [],
      });
    }
  }

  async refresh(): Promise<void> {
    const groupId = this.snapshot.activeGroupId;
    const entry = groupId ? this.snapshot.entries[groupId] : undefined;
    if (this.refreshing || !groupId || !entry || entry.historyStatus !== 'ready' || entry.loadingMore) return;
    const epoch = this.historyEpoch;
    const headCursor = entry.headCursor ?? null;
    this.refreshing = true;
    try {
      const page = await this.port.read.history(groupId, headCursor ?? undefined, headCursor ? 'after' : 'tail');
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      const current = this.snapshot.entries[groupId];
      if (!current) return;
      this.storeRawEntries(groupId, page.entries);
      this.mergeEntryNames(groupId, page.memberNames);
      this.entry(groupId, {
        messages: page.memberNames
          ? mergeGroupMessages([], this.rawEntries.get(groupId) ?? page.entries, groupId, this.context, this.port.identity)
          : mergeGroupMessages(current.messages, page.entries, groupId, this.context, this.port.identity),
        headCursor: headCursor ? page.nextCursor ?? page.headCursor ?? headCursor : page.headCursor ?? null,
        historyError: undefined,
        stale: undefined,
      });
    } catch (error) {
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      const current = this.snapshot.entries[groupId];
      if (!current) return;
      const code = errorCodeOf(error);
      if (code === 'CONVERSATION_NOT_MEMBER') {
        this.entry(groupId, {
          historyStatus: 'error',
          membershipStatus: 'not-member',
          historyError: '你已不在这个群里。',
          messages: [],
        });
        return;
      }
      if (code === 'CONVERSATION_NOT_FOUND') {
        this.entry(groupId, {
          historyStatus: 'error',
          membershipStatus: 'not-found',
          historyError: '这个群已不存在。',
          messages: [],
        });
        return;
      }
      if (code === 'DAEMON_UNAVAILABLE') {
        this.entry(groupId, {
          historyStatus: 'error',
          stale: current.messages.length > 0 ? true : undefined,
          historyError: `历史消息读取失败：${failureText(error)}`,
          messages: current.messages,
        });
        return;
      }
      this.entry(groupId, { historyError: `消息更新失败：${failureText(error)}` });
    } finally {
      this.refreshing = false;
    }
  }

  async loadMore(groupId: string): Promise<void> {
    const entry = this.snapshot.entries[groupId];
    if (this.snapshot.activeGroupId !== groupId || !entry || !entry.nextCursor
      || entry.loadingMore || entry.historyStatus !== 'ready') return;
    const epoch = this.historyEpoch;
    const cursor = entry.nextCursor;
    this.entry(groupId, { loadingMore: true, historyError: undefined });
    try {
      const page = await this.port.read.history(groupId, cursor, 'older');
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      const current = this.snapshot.entries[groupId];
      if (!current) return;
      this.storeRawEntries(groupId, page.entries);
      this.mergeEntryNames(groupId, page.memberNames);
      const nextOlder = page.nextOlderCursor ?? page.nextCursor;
      this.entry(groupId, {
        loadingMore: false,
        nextCursor: nextOlder === cursor ? null : nextOlder,
        messages: page.memberNames
          ? mergeGroupMessages([], this.rawEntries.get(groupId) ?? page.entries, groupId, this.context, this.port.identity)
          : mergeGroupMessages(current.messages, page.entries, groupId, this.context, this.port.identity),
      });
    } catch (error) {
      if (epoch !== this.historyEpoch || this.snapshot.activeGroupId !== groupId) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        this.entry(groupId, { loadingMore: false });
        this.port.reportUnavailable?.('读取消息', refusal);
        return;
      }
      this.entry(groupId, { loadingMore: false, historyError: `历史消息读取失败：${failureText(error)}` });
    }
  }

  async refreshList(): Promise<void> {
    await this.port.refreshDirectory?.();
  }

  resolveMemberName(groupId: string, memberId: string): string {
    return resolveGroupMemberName(memberId, {
      identity: this.port.identity,
      members: this.context.membersByGroup[groupId],
      targets: this.context.targets,
      entryNames: this.context.entryNamesByGroup[groupId],
    });
  }
}

/** Builds one group chat controller per injected capability generation. */
export function useGroupChat(
  port: GroupChatPort | undefined,
  groups: readonly GroupHead[],
  targets: readonly GroupTargetNameSource[],
): GroupChatController | null {
  const controller = useMemo(
    () => (port ? new GroupChatController(port) : null),
    [port],
  );
  useEffect(() => {
    controller?.setContext(groups, targets);
  }, [controller, groups, targets]);
  useEffect(() => {
    if (!controller) return;
    let cancelled = false;
    let reading = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      clearTimeout(timer);
      if (cancelled || reading || document.visibilityState === 'hidden') return;
      timer = setTimeout(async () => {
        reading = true;
        try { await controller.refresh(); }
        finally { reading = false; schedule(); }
      }, 2000);
    };
    schedule();
    document.addEventListener('visibilitychange', schedule);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  }, [controller]);
  return controller;
}
