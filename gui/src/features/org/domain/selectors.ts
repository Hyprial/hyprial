import { ORG_COPY } from './copy';
import type {
  OrgDetail,
  OrgDomainSnapshot,
  OrgSpace,
  OrgSummary,
  ReceivedInvite,
} from './types';

/** 个人组织固定第一位，工作组织按名称排序。 */
export function sortOrgs(orgs: readonly OrgSummary[]): readonly OrgSummary[] {
  const personal = orgs.filter((org) => org.kind === 'personal');
  const work = orgs
    .filter((org) => org.kind === 'work')
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
  return [...personal, ...work];
}

/** 还要本机处理的邀请：不含数据源标为已跳过的；未知时为 null。计数、提醒与登录后确认都用它。 */
export function pendingInvites(snapshot: OrgDomainSnapshot): readonly ReceivedInvite[] | null {
  return snapshot.receivedInvites?.filter((invite) => !invite.skipReason) ?? null;
}

export function orgDisplayName(org: OrgSummary): string {
  return org.kind === 'personal' ? ORG_COPY.personalName : org.name;
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

export function personalOrg(snapshot: OrgDomainSnapshot): OrgSummary | null {
  return snapshot.orgs?.find((org) => org.kind === 'personal') ?? null;
}

export function hasWorkOrg(snapshot: OrgDomainSnapshot): boolean {
  return snapshot.orgs?.some((org) => org.kind === 'work') ?? false;
}

export interface OrgPermissions {
  readonly invite: boolean;
  readonly removeMember: boolean;
  readonly approve: boolean;
  readonly configure: boolean;
  readonly rename: boolean;
  readonly purge: boolean;
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
    approve: work && owner,
    configure: work && owner,
    rename: work && owner,
    purge: owner,
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
