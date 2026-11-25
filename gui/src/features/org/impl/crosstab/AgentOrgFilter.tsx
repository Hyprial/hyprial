import { Select } from '../../../../components/primitives';
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

/** 组织视图按权威目录筛选；“全部”保留本机未关联 Agent。 */
export function useAgentOrgFilter<T extends { readonly id: string; readonly agentId?: string }>(
  agents: readonly T[],
  orgId: string,
): readonly T[] {
  const { provided, snapshot } = useOrgDomain();
  if (!provided || orgId === ALL_ORGS) {
    return agents;
  }
  if (snapshot.orgAgentDirectory !== undefined) {
    const records = snapshot.orgAgentDirectory[orgId] ?? [];
    const members = new Set(records.map((record) => record.id));
    return agents.filter((agent) => members.has(agent.agentId ?? agent.id));
  }
  if (!orgDataReady(provided, snapshot)) return agents;
  if (!snapshot.agentOrgs) return [];
  return agents.filter((agent) => snapshot.agentOrgs?.[agent.agentId ?? agent.id]?.includes(orgId));
}
