import { findOrg } from '../domain/selectors';
import type { OrgDomainSnapshot, OrgSummary } from '../domain/types';

/** 组织数据已就绪（有数据源且后端返回了组织列表）。 */
export function orgDataReady(provided: boolean, snapshot: OrgDomainSnapshot): boolean {
  return provided && snapshot.status === 'ready' && snapshot.orgs !== null;
}

/** Agent 所属组织（只在后端提供时使用）。 */
export function agentOrgs(snapshot: OrgDomainSnapshot, agentId: string): readonly OrgSummary[] | null {
  if (snapshot.agentOrgs === null) {
    return null;
  }
  const ids = snapshot.agentOrgs?.[agentId] ?? [];
  return ids
    .map((id) => findOrg(snapshot, id))
    .filter((org): org is OrgSummary => org !== null);
}
