import { ORG_COPY, zh } from '../domain/copy';
import { findOrg, orgDisplayName, personalOrg, sortOrgs } from '../domain/selectors';
import type { OrgDomainSnapshot, OrgSummary } from '../domain/types';

/** 组织数据已就绪（有数据源且后端返回了组织列表）。 */
export function orgDataReady(provided: boolean, snapshot: OrgDomainSnapshot): boolean {
  return provided && snapshot.status === 'ready' && snapshot.orgs !== null;
}

/** Agent 所属组织（只在后端提供时使用）。 */
export function agentOrgs(snapshot: OrgDomainSnapshot, agentId: string): readonly OrgSummary[] | null {
  const ids = snapshot.agentOrgs?.[agentId];
  if (!ids) {
    return null;
  }
  return ids
    .map((id) => findOrg(snapshot, id))
    .filter((org): org is OrgSummary => org !== null);
}

export interface AgentOrgChoice {
  readonly options: readonly { readonly id: string; readonly label: string }[];
  readonly defaultId: string;
  readonly note: string;
}

/**
 * 新建 Agent 的“所属组织”（第 7 节、B4）：第一项是个人组织；默认当前组织，
 * 没有当前组织时默认个人组织；不提供“不加入组织”。
 */
export function agentOrgChoice(
  snapshot: OrgDomainSnapshot,
  preferredOrgId?: string,
): AgentOrgChoice | null {
  if (!snapshot.orgs || snapshot.orgs.length === 0) {
    return null;
  }
  const orgs = sortOrgs(snapshot.orgs);
  const current = findOrg(snapshot, snapshot.currentOrgId);
  const fallback = current ?? personalOrg(snapshot) ?? orgs[0];
  const preferred = findOrg(snapshot, preferredOrgId);
  const chosen = preferred ?? fallback;
  const rule = current
    ? ORG_COPY.orgDefaultNote(orgDisplayName(current))
    : `${ORG_COPY.noCurrentOrg}，默认归入个人组织。`;
  return {
    options: orgs.map((org) => ({ id: org.id, label: orgDisplayName(org) })),
    defaultId: chosen.id,
    // 从组织页“在此组织新建 Agent”进入时，说明预选的组织，再说明默认规则。
    note: chosen.id === fallback.id ? rule : zh`已预选 ${orgDisplayName(chosen)}。${rule}`,
  };
}
