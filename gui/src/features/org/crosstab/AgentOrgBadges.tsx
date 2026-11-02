import type { ReactNode } from 'react';
import { Badge } from '../../../components/Badge';
import { ORG_COPY } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import { orgDisplayName } from '../domain/selectors';
import { agentOrgs, orgDataReady } from './agent-org';

/**
 * Agent 卡片上的组织标签：最多 2 个，其余显示“+n”（第 7 节）。
 * 按组织筛选时，命中的组织排第一并高亮，说明这张卡为什么在结果里。
 * 没有组织数据时显示 fallback（原有的单个组织标签）。
 */
export function AgentOrgBadges({
  agentId,
  fallback = null,
  priorityOrgId,
}: {
  readonly agentId: string;
  readonly fallback?: ReactNode;
  readonly priorityOrgId?: string;
}) {
  const { provided, snapshot } = useOrgDomain();
  if (!orgDataReady(provided, snapshot)) {
    return <>{fallback}</>;
  }
  const orgs = agentOrgs(snapshot, agentId);
  if (!orgs || orgs.length === 0) {
    return null;
  }
  const ordered = priorityOrgId
    ? [...orgs.filter((org) => org.id === priorityOrgId), ...orgs.filter((org) => org.id !== priorityOrgId)]
    : orgs;
  const shown = ordered.slice(0, 2);
  const hidden = ordered.slice(2);
  // 与 Agent 卡片上其他标签同一形态（Badge）；“+n”的组织名留给读屏。
  return (
    <>
      <span className="ui-visually-hidden">所属组织：</span>
      {shown.map((org) => (
        <Badge key={org.id} tone={org.id === priorityOrgId ? 'positive' : 'neutral'}>
          {orgDisplayName(org)}
        </Badge>
      ))}
      {hidden.length > 0 ? (
        <>
          <Badge tone="neutral">{`+${hidden.length}`}</Badge>
          <span className="ui-visually-hidden">
            （另有 {hidden.map(orgDisplayName).join('、')}）
          </span>
        </>
      ) : null}
    </>
  );
}

/** Agent 详情“所属组织”一行：组织数据就绪时列出全部所属组织，否则显示原值。 */
export function AgentOrgSummary({
  agentId,
  fallback,
}: {
  readonly agentId: string;
  readonly fallback: string;
}) {
  const { provided, snapshot } = useOrgDomain();
  const orgs = orgDataReady(provided, snapshot) ? agentOrgs(snapshot, agentId) : null;
  if (!orgs) {
    return <>{fallback}</>;
  }
  return <>{orgs.length > 0 ? orgs.map(orgDisplayName).join('、') : ORG_COPY.unknown}</>;
}
