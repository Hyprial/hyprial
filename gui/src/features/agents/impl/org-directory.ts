import type { ManagedAgentView } from '../../../state/agent-selectors';
import type { OrgAgentRecord } from '../../org';

export function mergeOrganizationAgents(
  local: readonly ManagedAgentView[],
  byOrg: Readonly<Record<string, readonly OrgAgentRecord[] | null>>,
): readonly ManagedAgentView[] {
  const listed = new Set(local.map((agent) => agent.agentId ?? agent.id));
  const remote: ManagedAgentView[] = [];
  const unknown = { kind: 'unknown' as const, label: '状态未确认', sourceLabel: '状态未确认' };
  for (const records of Object.values(byOrg)) {
    for (const record of records ?? []) {
      if (listed.has(record.id)) continue;
      listed.add(record.id);
      remote.push({
        id: record.id,
        agentId: record.id,
        remote: true,
        displayName: record.name,
        role: '',
        avatarTone: 'grey',
        modelServiceId: null,
        channelServiceId: null,
        organizationId: record.orgIds[0] ?? null,
        modelLabel: '状态未确认',
        capabilities: [],
        organizationLabel: '组织',
        ownerLabel: record.ownerLabel,
        host: record.machineLabel,
        availability: 'unknown',
        model: unknown,
        channel: unknown,
      });
    }
  }
  return [...local, ...remote];
}
