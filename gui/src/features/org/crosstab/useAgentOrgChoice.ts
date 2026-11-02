import { useEffect, useState } from 'react';
import { useOrgDomain } from '../domain/context';
import { agentOrgChoice, orgDataReady, type AgentOrgChoice } from './agent-org';

/**
 * 新建 Agent 表单的“所属组织”选项（第 7 节、B4）。只在组织数据就绪时返回；
 * 否则返回 null，表单保持原有选项。从组织页“在此组织新建 Agent”进入时预选该组织。
 */
export function useAgentCreateOrgChoice(active: boolean): AgentOrgChoice | null {
  const { provided, snapshot, agentIntent, clearAgentIntent } = useOrgDomain();
  const [preferredOrgId] = useState(() => agentIntent?.createInOrgId);

  useEffect(() => {
    if (active && agentIntent?.createInOrgId) {
      clearAgentIntent();
    }
  }, [active, agentIntent, clearAgentIntent]);

  if (!active || !orgDataReady(provided, snapshot)) {
    return null;
  }
  return agentOrgChoice(snapshot, preferredOrgId);
}
