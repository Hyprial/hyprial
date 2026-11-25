import { ORG_COPY } from './copy';
import type {
  OrgDetail,
  OrgDomainSnapshot,
  OrgPerson,
  OrgSpace,
  OrgSummary,
  ReceivedInvite,
} from './types';

/** 工作组织按名称排序。 */
export function sortOrgs(orgs: readonly OrgSummary[]): readonly OrgSummary[] {
  return orgs
    .filter((org) => org.kind === 'work')
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** 还要本机处理的邀请：不含数据源标为已跳过的；未知时为 null。计数、提醒与登录后确认都用它。 */
export function pendingInvites(snapshot: OrgDomainSnapshot): readonly ReceivedInvite[] | null {
  return snapshot.receivedInvites?.filter((invite) => !invite.skipReason) ?? null;
}

export function orgDisplayName(org: OrgSummary): string {
  return org.name;
}

/**
 * 组织 id 的展示名：只剥一次 `group-` 前缀，与 CLI 的同一规则一致。
 * id 本身保留完整组织名；这里只用于显示，不改变任何请求参数。
 */
export function orgNameFromId(org: string): string {
  return org.startsWith('group-') ? org.slice('group-'.length) : org;
}

export function findOrg(
  snapshot: OrgDomainSnapshot,
  orgId: string | null | undefined,
): OrgSummary | null {
  if (!orgId || !snapshot.orgs) {
    return null;
  }
  return snapshot.orgs.find((org) => org.id === orgId) ?? null;
}

export function orgDetail(
  snapshot: OrgDomainSnapshot,
  orgId: string | null | undefined,
): OrgDetail | null {
  if (!orgId) {
    return null;
  }
  return snapshot.details[orgId] ?? null;
}

export interface OrgPermissions {
  readonly invite: boolean;
  readonly removeMember: boolean;
  readonly configure: boolean;
  readonly rename: boolean;
  readonly leave: boolean;
  readonly leaveBlockedReason: string | null;
  readonly deleteOrg: boolean;
  readonly adopt: boolean;
}

/**
 * 按钮可见性。角色与 authority 标记来自后端，GUI 只据此决定显示，不在本地推断权限。
 */
export function orgPermissions(
  org: OrgSummary | null,
  snapshot: OrgDomainSnapshot,
): OrgPermissions {
  const work = org?.kind === 'work';
  const owner = org?.role === 'owner';
  return {
    invite: work && owner,
    removeMember: work && owner,
    configure: work && owner,
    rename: work && owner,
    leave: work,
    leaveBlockedReason: work && org?.authorityDevice ? ORG_COPY.authorityCannotLeave : null,
    deleteOrg: work && owner,
    adopt: snapshot.nodeOwner,
  };
}

/** 空间写操作的禁用原因；null 表示可写。 */
export function spaceWriteBlock(space: OrgSpace | null): string | null {
  if (!space) {
    return ORG_COPY.spaceUnknownReason;
  }
  if (space.health === 'unknown') {
    return ORG_COPY.spaceUnknownReason;
  }
  if (space.health === 'history-anomaly') {
    return ORG_COPY.historyAnomalyReason;
  }
  if (!space.writable) {
    return ORG_COPY.noWriteReason;
  }
  return null;
}

export function countLabel(items: readonly unknown[] | null): string {
  return items === null ? ORG_COPY.unknown : String(items.length);
}

/**
 * `orgfs.members` 与 `org.network` 对 owner 的写法可能是 `user:x` 或 `x`；
 * 只做这一层前缀归一，不推断其他身份。
 */
export function sameUser(left: string, right: string | null | undefined): boolean {
  if (right === null || right === undefined) return false;
  const bare = (value: string) =>
    value.startsWith('user:') ? value.slice('user:'.length) : value;
  return bare(left) === bare(right);
}

/** 成员页显示名/账号去掉 `user:` 前缀；id 保留原始 URI 供后端动作使用。 */
function displayUser(value: string): string {
  return value.startsWith('user:') ? value.slice('user:'.length) : value;
}

/**
 * 把 orgfs.members 映射为成员页的人列表；读不到成员、无法确定 space owner，
 * 或无法确定 self owner（org.network 失败/self.owner 缺失）时返回 null。
 * 后两种情况会让 isMe 和 machineIds 变成未知，不能伪装成“不是我/没有机器”，
 * 因此整份成员列表保持 UnknownBlock。已知 Agent 归属按 owner 关联到成员。
 */
export function peopleFromMembers(input: {
  readonly members: readonly { readonly user: string }[] | null;
  readonly spaceOwner: string | null;
  readonly devices: readonly { readonly deviceId: string; readonly owner: string }[];
  readonly selfOwner: string | null | undefined;
  readonly agents?: readonly { readonly id: string; readonly ownerLabel: string }[] | null;
}): OrgPerson[] | null {
  if (
    input.members === null ||
    input.spaceOwner === null ||
    input.selfOwner === null ||
    input.selfOwner === undefined
  ) {
    return null;
  }
  return input.members.map((member) => ({
    id: member.user,
    displayName: displayUser(member.user),
    account: displayUser(member.user),
    role: sameUser(member.user, input.spaceOwner) ? 'owner' : 'member',
    source: 'orgfs',
    sync: 'synced',
    isMe: sameUser(member.user, input.selfOwner),
    machineIds: input.devices
      .filter((device) => sameUser(device.owner, member.user))
      .map((device) => device.deviceId),
    agentIds: (input.agents ?? [])
      .filter((agent) => sameUser(agent.ownerLabel, member.user))
      .map((agent) => agent.id),
  }));
}
