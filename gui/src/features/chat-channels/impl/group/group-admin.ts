/**
 * Decision record (group chat admin adapter & controller, slice GC5-3, 2026-11-21):
 * - Adapts `conversations.create`, `conversations.get`, `conversations.members.add`,
 *   and `conversations.members.remove` via `createGroupAdminApiFromClient` with
 *   named methods so `capabilities.tsx` adds zero inline request arrows.
 * - Each write action (`create`, `addMember`, `removeMember`) generates one
 *   43-char base64url `idempotencyKey` per click in memory only. When a response
 *   is lost (`dispatch === 'unknown'`), `DAEMON_UNAVAILABLE` occurs, or the
 *   response fails validation, the state stays `'unknown'`（「结果未知。」）with
 *   the original key retained so explicit retry replays the exact same key.
 * - Member mutations update the cached member list directly from the response
 *   without issuing a follow-up `conversations.get` read (1 action = 1 request).
 * - Group creation does not auto-open the new group (owner decision D4); instead
 *   the newly created group is prepended to `createdGroups` and marked in
 *   `highlightedGroupId` until selected.
 * - Candidate members are computed purely in memory as the intersection of chat
 *   targets carrying `agentId` and the local agent directory.
 * - Slice GC5-4 (2026-11-21): `executeCreate` seeds `groups[conversationId]`
 *   with the created `detail` and `ensureMembersLoaded` reuses cached `ready` /
 *   in-flight `loading` entries so `@` picker reads `conversations.get` at most
 *   once per uncached group.
 */
import { useMemo } from 'react';
import {
  ClientError,
  MethodRefusedError,
  assertMethodAvailable,
  methodRefusal,
  type MethodAccess,
} from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';
import type { UnavailableDetail } from '../../../../components/notifications';
import type { GroupHead } from '../message-read-api';

const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export interface GroupMember {
  readonly memberId: string;
  readonly kind: 'user' | 'agent';
  readonly displayName: string;
  readonly manager: boolean;
  readonly joinedAtMs: number;
  readonly addedBy: string;
  readonly address?: string;
}

export interface GroupDetail {
  readonly conversationId: string;
  readonly title: string;
  readonly createdBy: string;
  readonly createdAtMs: number;
  readonly members: readonly GroupMember[];
  readonly self?: string;
  readonly canManageMembers?: boolean;
}

export interface GroupMembersMutationResult {
  readonly conversationId: string;
  readonly members: readonly GroupMember[];
  readonly entryId: string | null;
}

export interface GroupMemberCandidate {
  readonly memberId: string;
  readonly displayName: string;
  readonly uri: string;
}

export interface GroupAdminApi {
  createGroup(params: {
    readonly title: string;
    readonly members: readonly string[];
    readonly idempotencyKey: string;
  }): Promise<GroupDetail>;
  getGroup(conversationId: string): Promise<GroupDetail>;
  addMember(params: {
    readonly conversationId: string;
    readonly member: string;
    readonly idempotencyKey: string;
  }): Promise<GroupMembersMutationResult>;
  removeMember(params: {
    readonly conversationId: string;
    readonly memberId: string;
    readonly idempotencyKey: string;
  }): Promise<GroupMembersMutationResult>;
}

export interface GroupAdminPort {
  readonly api: GroupAdminApi;
  readonly reportUnavailable?: (subject: string, detail: UnavailableDetail) => void;
  readonly onGroupMembersUpdated?: (groupId: string, members: readonly GroupMember[]) => void;
  readonly randomBytes?: (count: number) => Uint8Array;
}

export type GroupCreateMutationState =
  | { readonly status: 'idle' }
  | { readonly status: 'pending'; readonly idempotencyKey: string; readonly title: string; readonly members: readonly string[] }
  | { readonly status: 'rejected'; readonly code?: string; readonly message: string }
  | { readonly status: 'unknown'; readonly idempotencyKey: string; readonly title: string; readonly members: readonly string[]; readonly message: string };

export type GroupAddMemberState =
  | { readonly status: 'idle' }
  | { readonly status: 'pending'; readonly groupId: string; readonly member: string; readonly idempotencyKey: string }
  | { readonly status: 'rejected'; readonly groupId: string; readonly code?: string; readonly message: string }
  | { readonly status: 'unknown'; readonly groupId: string; readonly member: string; readonly idempotencyKey: string; readonly message: string };

export type GroupRemoveMemberState =
  | { readonly status: 'idle' }
  | { readonly status: 'pending'; readonly groupId: string; readonly memberId: string; readonly idempotencyKey: string }
  | { readonly status: 'rejected'; readonly groupId: string; readonly memberId: string; readonly code?: string; readonly message: string }
  | { readonly status: 'unknown'; readonly groupId: string; readonly memberId: string; readonly idempotencyKey: string; readonly message: string };

export type GroupMembersEntryState =
  | { readonly status: 'loading'; readonly detail?: GroupDetail }
  | { readonly status: 'ready'; readonly detail: GroupDetail }
  | { readonly status: 'error'; readonly message: string; readonly code?: string; readonly detail?: GroupDetail };

export interface GroupAdminSnapshot {
  readonly create: GroupCreateMutationState;
  readonly addMember: GroupAddMemberState;
  readonly removeMember: GroupRemoveMemberState;
  readonly groups: Readonly<Record<string, GroupMembersEntryState>>;
  readonly createdGroups: readonly GroupHead[];
  readonly highlightedGroupId: string | null;
  readonly memberCountOverrides: Readonly<Record<string, number>>;
}

export const EMPTY_GROUP_ADMIN_SNAPSHOT: GroupAdminSnapshot = {
  create: { status: 'idle' },
  addMember: { status: 'idle' },
  removeMember: { status: 'idle' },
  groups: {},
  createdGroups: [],
  highlightedGroupId: null,
  memberCountOverrides: {},
};

export const GROUP_ADMIN_REFUSALS: Readonly<Record<string, string>> = {
  AGENT_NOT_FOUND: '找不到这个 Agent（可能已删除或不在本机）。',
  UNSUPPORTED_TARGET: '不能将其他所有者的对象加入群聊。',
  INVALID_ARGUMENT: '请求参数无效，请检查名称或成员选择。',
  CALLER_NOT_AUTHORIZED: '当前身份无权执行此群聊操作。',
  CONVERSATION_NOT_FOUND: '这个群已不存在。',
  CONVERSATION_NOT_MEMBER: '你已不在这个群里。',
  MOCK_EXAMPLE_MISSING: '当前预览示例未覆盖这组参数。',
  SENDER_UNRESOLVED: '无法确认操作者身份。',
  SENDER_NOT_AUTHENTICATED: '操作者身份未通过验证。',
  DAEMON_NOT_READY: '守护进程尚未就绪，请稍后再试。',
};

export class GroupResponseMismatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GroupResponseMismatchError';
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function nonempty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function parseGroupMember(raw: unknown): GroupMember {
  if (!isObject(raw)) throw new GroupResponseMismatchError('群成员格式不正确');
  if (!nonempty(raw.memberId) || (raw.kind !== 'user' && raw.kind !== 'agent')
    || typeof raw.displayName !== 'string' || typeof raw.manager !== 'boolean'
    || !finiteNumber(raw.joinedAtMs) || !nonempty(raw.addedBy)) {
    throw new GroupResponseMismatchError('群成员字段不完整');
  }
  if (raw.address !== undefined && !nonempty(raw.address)) {
    throw new GroupResponseMismatchError('群成员地址格式不正确');
  }
  return {
    memberId: raw.memberId,
    kind: raw.kind,
    displayName: raw.displayName,
    manager: raw.manager,
    joinedAtMs: raw.joinedAtMs,
    addedBy: raw.addedBy,
    ...(raw.address === undefined ? {} : { address: raw.address }),
  };
}

function parseGroupDetail(raw: unknown, requireGetFields: boolean, expectedGroupId?: string): GroupDetail {
  if (!isObject(raw) || raw.ok !== true || raw.kind !== 'group' || !nonempty(raw.conversationId)
    || typeof raw.title !== 'string' || !nonempty(raw.createdBy) || !finiteNumber(raw.createdAtMs)
    || !Array.isArray(raw.members)) {
    throw new GroupResponseMismatchError('群聊信息格式不正确');
  }
  if (expectedGroupId !== undefined && raw.conversationId !== expectedGroupId) {
    throw new GroupResponseMismatchError('群聊信息 ID 不一致');
  }
  if (requireGetFields && (!nonempty(raw.self) || typeof raw.canManageMembers !== 'boolean')) {
    throw new GroupResponseMismatchError('群成员权限字段缺失');
  }
  return {
    conversationId: raw.conversationId,
    title: raw.title,
    createdBy: raw.createdBy,
    createdAtMs: raw.createdAtMs,
    members: raw.members.map(parseGroupMember),
    ...(nonempty(raw.self) ? { self: raw.self } : {}),
    ...(typeof raw.canManageMembers === 'boolean' ? { canManageMembers: raw.canManageMembers } : {}),
  };
}

function parseMembersMutationResult(raw: unknown, expectedGroupId: string): GroupMembersMutationResult {
  if (!isObject(raw) || raw.ok !== true || raw.conversationId !== expectedGroupId
    || !Array.isArray(raw.members) || !(raw.entryId === null || nonempty(raw.entryId))) {
    throw new GroupResponseMismatchError('群成员变更应答格式不正确');
  }
  return {
    conversationId: raw.conversationId,
    members: raw.members.map(parseGroupMember),
    entryId: raw.entryId,
  };
}

export function createGroupIdempotencyKey(
  randomBytes: (count: number) => Uint8Array = (count) => globalThis.crypto.getRandomValues(new Uint8Array(count)),
): string {
  const bytes = randomBytes(32);
  if (!(bytes instanceof Uint8Array) || bytes.length !== 32) throw new Error('随机源无效');
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const key = btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  if (!IDEMPOTENCY_KEY_PATTERN.test(key)) throw new Error('幂等键格式无效');
  return key;
}

export function createGroupAdminApiFromClient(
  client: Partial<Pick<GeneratedClient, 'conversations'>>,
  access?: MethodAccess,
): GroupAdminApi {
  return {
    async createGroup(params) {
      const title = params.title.trim();
      if (!title || !nonempty(params.idempotencyKey)) throw new Error('群聊名称或操作键缺失');
      if (!client.conversations) throw new MethodRefusedError('conversations.create', 'GUI_CAPABILITY_UNAVAILABLE');
      const request = { title, members: [...params.members], idempotencyKey: params.idempotencyKey };
      assertMethodAvailable(access, 'conversations.create', request);
      const result = await client.conversations.create(request);
      return parseGroupDetail(result, false);
    },
    async getGroup(conversationId) {
      if (!nonempty(conversationId)) throw new Error('群聊 ID 缺失');
      if (!client.conversations) throw new MethodRefusedError('conversations.get', 'GUI_CAPABILITY_UNAVAILABLE');
      const request = { conversationId };
      assertMethodAvailable(access, 'conversations.get', request);
      const result = await client.conversations.get(request);
      return parseGroupDetail(result, true, conversationId);
    },
    async addMember(params) {
      if (!nonempty(params.conversationId) || !nonempty(params.member) || !nonempty(params.idempotencyKey)) {
        throw new Error('群聊添加成员参数不完整');
      }
      if (!client.conversations) throw new MethodRefusedError('conversations.members.add', 'GUI_CAPABILITY_UNAVAILABLE');
      const request = {
        conversationId: params.conversationId,
        member: params.member,
        idempotencyKey: params.idempotencyKey,
      };
      assertMethodAvailable(access, 'conversations.members.add', request);
      const result = await client.conversations.members.add(request);
      return parseMembersMutationResult(result, params.conversationId);
    },
    async removeMember(params) {
      if (!nonempty(params.conversationId) || !nonempty(params.memberId) || !nonempty(params.idempotencyKey)) {
        throw new Error('群聊移除成员参数不完整');
      }
      if (!client.conversations) throw new MethodRefusedError('conversations.members.remove', 'GUI_CAPABILITY_UNAVAILABLE');
      const request = {
        conversationId: params.conversationId,
        memberId: params.memberId,
        idempotencyKey: params.idempotencyKey,
      };
      assertMethodAvailable(access, 'conversations.members.remove', request);
      const result = await client.conversations.members.remove(request);
      return parseMembersMutationResult(result, params.conversationId);
    },
  };
}

export function buildGroupMemberCandidates(
  targets: readonly {
    readonly id?: string;
    readonly targetUri?: string;
    readonly kind?: string;
    readonly agentId?: string | null;
    readonly displayName?: string | null;
  }[],
  localAgents: readonly {
    readonly uri: string;
    readonly agentId?: string | null;
    readonly displayName?: string;
  }[],
  excludeMemberIds: readonly string[] = [],
): readonly GroupMemberCandidate[] {
  const localUris = new Set(localAgents.map((agent) => agent.uri));
  const localAgentIds = new Set(localAgents.flatMap((agent) => (agent.agentId ? [agent.agentId] : [])));
  const excluded = new Set(excludeMemberIds);
  const seen = new Set<string>();
  const candidates: GroupMemberCandidate[] = [];
  for (const target of targets) {
    if (target.kind !== undefined && target.kind !== 'agent') continue;
    const memberId = typeof target.agentId === 'string' ? target.agentId.trim() : '';
    if (!memberId) continue;
    const uri = target.targetUri ?? target.id ?? '';
    const isLocal = (uri.length > 0 && localUris.has(uri)) || localAgentIds.has(memberId);
    if (!isLocal || excluded.has(memberId) || seen.has(memberId)) continue;
    seen.add(memberId);
    candidates.push({
      memberId,
      displayName: target.displayName?.trim() || memberId,
      uri: uri || memberId,
    });
  }
  return candidates;
}

type WriteErrorClassification =
  | { readonly kind: 'refused-before-dispatch'; readonly refusal: NonNullable<ReturnType<typeof methodRefusal>> }
  | { readonly kind: 'unknown'; readonly message: string }
  | { readonly kind: 'rejected'; readonly code?: string; readonly message: string };

function formatClientRejection(error: ClientError, fallback: string): string {
  const base = GROUP_ADMIN_REFUSALS[error.code] ?? fallback;
  const requestId = error.diagnostics.requestId;
  const suffix = requestId ? `${error.code}，请求 ${requestId}` : error.code;
  return `${base}（${suffix}）`;
}

export function classifyGroupAdminWriteError(error: unknown, fallback: string): WriteErrorClassification {
  const refusal = methodRefusal(error);
  if (refusal !== null) return { kind: 'refused-before-dispatch', refusal };
  if (error instanceof GroupResponseMismatchError) {
    return { kind: 'unknown', message: '结果未知。' };
  }
  if (error instanceof ClientError) {
    if (error.dispatch === 'unknown' || error.code === 'DAEMON_UNAVAILABLE') {
      return { kind: 'unknown', message: '结果未知。' };
    }
    return {
      kind: 'rejected',
      code: error.code,
      message: formatClientRejection(error, fallback),
    };
  }
  return { kind: 'unknown', message: '结果未知。' };
}

function classifyGroupGetError(error: unknown): {
  readonly code?: string;
  readonly message: string;
} {
  if (error instanceof ClientError) {
    if (error.code === 'CONVERSATION_NOT_MEMBER') return { code: error.code, message: '你已不在这个群里。' };
    if (error.code === 'CONVERSATION_NOT_FOUND') return { code: error.code, message: '这个群已不存在。' };
    if (error.code === 'DAEMON_UNAVAILABLE') return { code: error.code, message: '当前无法读取成员名单，请稍后再试。' };
    return { code: error.code, message: formatClientRejection(error, '成员名单读取失败') };
  }
  return { message: error instanceof Error && error.message ? error.message : '成员名单读取失败' };
}

export class GroupAdminController {
  private snapshot: GroupAdminSnapshot = EMPTY_GROUP_ADMIN_SNAPSHOT;
  private listeners = new Set<() => void>();
  private loadEpochByGroup = new Map<string, number>();

  constructor(private readonly port: GroupAdminPort) {}

  getSnapshot = (): GroupAdminSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(change: Partial<GroupAdminSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...change };
    this.listeners.forEach((listener) => listener());
  }

  private setGroupEntry(groupId: string, entry: GroupMembersEntryState): void {
    this.update({
      groups: { ...this.snapshot.groups, [groupId]: entry },
    });
  }

  private applyMembersMutation(groupId: string, members: readonly GroupMember[]): void {
    const current = this.snapshot.groups[groupId];
    const nextGroups = current?.detail
      ? {
        ...this.snapshot.groups,
        [groupId]: { status: 'ready' as const, detail: { ...current.detail, members } },
      }
      : this.snapshot.groups;
    const nextCreatedGroups = this.snapshot.createdGroups.map((head) =>
      head.conversationId === groupId ? { ...head, memberCount: members.length } : head,
    );
    this.update({
      groups: nextGroups,
      createdGroups: nextCreatedGroups,
      memberCountOverrides: { ...this.snapshot.memberCountOverrides, [groupId]: members.length },
    });
    this.port.onGroupMembersUpdated?.(groupId, members);
  }

  clearHighlight(groupId?: string): void {
    if (!this.snapshot.highlightedGroupId) return;
    if (groupId !== undefined && this.snapshot.highlightedGroupId !== groupId) return;
    this.update({ highlightedGroupId: null });
  }

  dismissCreateState(): void {
    if (this.snapshot.create.status === 'pending' || this.snapshot.create.status === 'idle') return;
    this.update({ create: { status: 'idle' } });
  }

  dismissAddMemberState(): void {
    if (this.snapshot.addMember.status === 'pending' || this.snapshot.addMember.status === 'idle') return;
    this.update({ addMember: { status: 'idle' } });
  }

  dismissRemoveMemberState(): void {
    if (this.snapshot.removeMember.status === 'pending' || this.snapshot.removeMember.status === 'idle') return;
    this.update({ removeMember: { status: 'idle' } });
  }

  async createGroup(title: string, members: readonly string[]): Promise<GroupDetail | null> {
    if (this.snapshot.create.status === 'pending') return null;
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      this.update({ create: { status: 'rejected', message: '请输入群聊名称。' } });
      return null;
    }
    const idempotencyKey = createGroupIdempotencyKey(this.port.randomBytes);
    return this.executeCreate(trimmedTitle, [...members], idempotencyKey);
  }

  async retryCreateGroup(): Promise<GroupDetail | null> {
    if (this.snapshot.create.status !== 'unknown') return null;
    const { title, members, idempotencyKey } = this.snapshot.create;
    return this.executeCreate(title, [...members], idempotencyKey);
  }

  private async executeCreate(
    title: string,
    members: readonly string[],
    idempotencyKey: string,
  ): Promise<GroupDetail | null> {
    this.update({
      create: { status: 'pending', idempotencyKey, title, members },
    });
    try {
      const detail = await this.port.api.createGroup({ title, members, idempotencyKey });
      const head: GroupHead = {
        conversationId: detail.conversationId,
        title: detail.title,
        memberCount: detail.members.length,
        lastMessageAtMs: detail.createdAtMs,
      };
      const remaining = this.snapshot.createdGroups.filter((row) => row.conversationId !== detail.conversationId);
      this.update({
        create: { status: 'idle' },
        createdGroups: [head, ...remaining],
        highlightedGroupId: detail.conversationId,
        groups: { ...this.snapshot.groups, [detail.conversationId]: { status: 'ready', detail } },
        memberCountOverrides: { ...this.snapshot.memberCountOverrides, [detail.conversationId]: detail.members.length },
      });
      this.port.onGroupMembersUpdated?.(detail.conversationId, detail.members);
      return detail;
    } catch (error) {
      const classified = classifyGroupAdminWriteError(error, '创建群聊失败');
      if (classified.kind === 'refused-before-dispatch') {
        this.update({ create: { status: 'idle' } });
        this.port.reportUnavailable?.('新建群聊', classified.refusal);
        return null;
      }
      if (classified.kind === 'unknown') {
        this.update({
          create: { status: 'unknown', idempotencyKey, title, members, message: classified.message },
        });
        return null;
      }
      this.update({
        create: { status: 'rejected', code: classified.code, message: classified.message },
      });
      return null;
    }
  }

  async ensureMembersLoaded(groupId: string): Promise<GroupDetail | null> {
    if (!groupId) return null;
    const existing = this.snapshot.groups[groupId];
    if (existing?.status === 'ready') return existing.detail;
    if (existing?.status === 'loading') return null;
    return this.loadMembers(groupId);
  }

  async loadMembers(groupId: string): Promise<GroupDetail | null> {
    if (!groupId) return null;
    const epoch = (this.loadEpochByGroup.get(groupId) ?? 0) + 1;
    this.loadEpochByGroup.set(groupId, epoch);
    const prior = this.snapshot.groups[groupId]?.detail;
    this.setGroupEntry(groupId, { status: 'loading', ...(prior ? { detail: prior } : {}) });
    try {
      const detail = await this.port.api.getGroup(groupId);
      if (this.loadEpochByGroup.get(groupId) !== epoch) return null;
      this.setGroupEntry(groupId, { status: 'ready', detail });
      this.update({
        memberCountOverrides: { ...this.snapshot.memberCountOverrides, [groupId]: detail.members.length },
      });
      this.port.onGroupMembersUpdated?.(groupId, detail.members);
      return detail;
    } catch (error) {
      if (this.loadEpochByGroup.get(groupId) !== epoch) return null;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        if (prior) {
          this.setGroupEntry(groupId, { status: 'ready', detail: prior });
        } else {
          const next = { ...this.snapshot.groups };
          delete next[groupId];
          this.update({ groups: next });
        }
        this.port.reportUnavailable?.('查看群成员', refusal);
        return null;
      }
      const classified = classifyGroupGetError(error);
      this.setGroupEntry(groupId, {
        status: 'error',
        message: classified.message,
        ...(classified.code ? { code: classified.code } : {}),
        ...(prior ? { detail: prior } : {}),
      });
      return null;
    }
  }

  async addMember(groupId: string, member: string): Promise<GroupMembersMutationResult | null> {
    if (this.snapshot.addMember.status === 'pending' || !groupId) return null;
    if (!member.trim()) {
      this.update({ addMember: { status: 'rejected', groupId, message: '请选择要添加的成员。' } });
      return null;
    }
    const idempotencyKey = createGroupIdempotencyKey(this.port.randomBytes);
    return this.executeAddMember(groupId, member, idempotencyKey);
  }

  async retryAddMember(): Promise<GroupMembersMutationResult | null> {
    if (this.snapshot.addMember.status !== 'unknown') return null;
    const { groupId, member, idempotencyKey } = this.snapshot.addMember;
    return this.executeAddMember(groupId, member, idempotencyKey);
  }

  private async executeAddMember(
    groupId: string,
    member: string,
    idempotencyKey: string,
  ): Promise<GroupMembersMutationResult | null> {
    this.update({
      addMember: { status: 'pending', groupId, member, idempotencyKey },
    });
    try {
      const result = await this.port.api.addMember({ conversationId: groupId, member, idempotencyKey });
      this.applyMembersMutation(groupId, result.members);
      this.update({ addMember: { status: 'idle' } });
      return result;
    } catch (error) {
      const classified = classifyGroupAdminWriteError(error, '添加成员失败');
      if (classified.kind === 'refused-before-dispatch') {
        this.update({ addMember: { status: 'idle' } });
        this.port.reportUnavailable?.('添加群成员', classified.refusal);
        return null;
      }
      if (classified.kind === 'unknown') {
        this.update({
          addMember: { status: 'unknown', groupId, member, idempotencyKey, message: classified.message },
        });
        return null;
      }
      this.update({
        addMember: { status: 'rejected', groupId, code: classified.code, message: classified.message },
      });
      return null;
    }
  }

  async removeMember(groupId: string, memberId: string): Promise<GroupMembersMutationResult | null> {
    if (this.snapshot.removeMember.status === 'pending' || !groupId || !memberId) return null;
    const idempotencyKey = createGroupIdempotencyKey(this.port.randomBytes);
    return this.executeRemoveMember(groupId, memberId, idempotencyKey);
  }

  async retryRemoveMember(): Promise<GroupMembersMutationResult | null> {
    if (this.snapshot.removeMember.status !== 'unknown') return null;
    const { groupId, memberId, idempotencyKey } = this.snapshot.removeMember;
    return this.executeRemoveMember(groupId, memberId, idempotencyKey);
  }

  private async executeRemoveMember(
    groupId: string,
    memberId: string,
    idempotencyKey: string,
  ): Promise<GroupMembersMutationResult | null> {
    this.update({
      removeMember: { status: 'pending', groupId, memberId, idempotencyKey },
    });
    try {
      const result = await this.port.api.removeMember({ conversationId: groupId, memberId, idempotencyKey });
      this.applyMembersMutation(groupId, result.members);
      this.update({ removeMember: { status: 'idle' } });
      return result;
    } catch (error) {
      const classified = classifyGroupAdminWriteError(error, '移除成员失败');
      if (classified.kind === 'refused-before-dispatch') {
        this.update({ removeMember: { status: 'idle' } });
        this.port.reportUnavailable?.('移除群成员', classified.refusal);
        return null;
      }
      if (classified.kind === 'unknown') {
        this.update({
          removeMember: { status: 'unknown', groupId, memberId, idempotencyKey, message: classified.message },
        });
        return null;
      }
      this.update({
        removeMember: { status: 'rejected', groupId, memberId, code: classified.code, message: classified.message },
      });
      return null;
    }
  }
}

export function useGroupAdmin(port: GroupAdminPort | undefined): GroupAdminController | null {
  return useMemo(() => (port ? new GroupAdminController(port) : null), [port]);
}
