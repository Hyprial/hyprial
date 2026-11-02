import { Select } from '../../../components/primitives';
import { useOrgDomain } from '../domain/context';
import { orgDisplayName, sortOrgs } from '../domain/selectors';
import { orgDataReady } from './agent-org';

/** “全部组织”。与同排的所有者、主机筛选一致，用空值表示不筛选。 */
export const ALL_ORGS = '';

/**
 * 按组织筛选（第 7 节）：查看某个组织内可以联系的 Agent。放在所有者、主机筛选下方，
 * 同一形态（Select 基件）；没有组织数据时不渲染。
 */
export function AgentOrgFilter({
  value,
  onChange,
}: {
  readonly value: string;
  readonly onChange: (orgId: string) => void;
}) {
  const { provided, snapshot } = useOrgDomain();
  if (!orgDataReady(provided, snapshot) || !snapshot.orgs || snapshot.orgs.length === 0) {
    return null;
  }
  return (
    <div className="org-agent-filter">
      <label htmlFor="agent-org-filter">组织</label>
      <Select
        id="agent-org-filter"
        className="org-select-inline"
        value={value}
        options={[
          { value: ALL_ORGS, label: '全部' },
          ...sortOrgs(snapshot.orgs).map((org) => ({ value: org.id, label: orgDisplayName(org) })),
        ]}
        onValueChange={onChange}
      />
    </div>
  );
}

/** 按所选组织过滤 Agent；没有组织数据或选“全部”时原样返回。 */
export function useAgentOrgFilter<T extends { readonly id: string }>(
  agents: readonly T[],
  orgId: string,
): readonly T[] {
  const { provided, snapshot } = useOrgDomain();
  if (orgId === ALL_ORGS || !orgDataReady(provided, snapshot) || !snapshot.agentOrgs) {
    return agents;
  }
  return agents.filter((agent) => snapshot.agentOrgs?.[agent.id]?.includes(orgId));
}
