/**
 * O1: join existing read models by exact identity, never by a label or URI segment.
 * Organization facts are limited to the readable snapshot; unavailable is not empty.
 */
import type { ChatTargetInfo } from '../features/agent-chat';
import type { AgentDirectoryRow } from '../features/agents';
import { orgDisplayName, useOrgDomain, type OrgDomainSnapshot } from '../features/org';
import type { SessionTarget } from '../state/gui-view-model';

type DirectoryRow = Pick<AgentDirectoryRow, 'uri' | 'owner' | 'host'>;

export function chatTargetInfo(
  target: SessionTarget | undefined,
  directory: readonly DirectoryRow[],
  snapshot?: OrgDomainSnapshot,
): ChatTargetInfo | undefined {
  if (!target) return undefined;
  const row = directory.find(candidate => candidate.uri === target.id);
  const visible = snapshot?.status === 'ready' ? snapshot.orgs : null;
  const orgAgent = visible?.flatMap(org => snapshot?.details[org.id]?.agents ?? [])
    .find(agent => agent.chatTargetId === target.id);
  const agentId = target.agentId ?? orgAgent?.id;
  const memberships = visible && agentId ? snapshot?.agentOrgs?.[agentId] : undefined;
  return {
    targetId: target.id,
    host: row?.host || orgAgent?.machineLabel || undefined,
    owner: row?.owner || orgAgent?.ownerLabel || undefined,
    organizations: memberships ? visible?.filter(org => memberships.includes(org.id)).map(orgDisplayName) : undefined,
  };
}

export function useChatTargetInfo(target: SessionTarget | undefined, directory: readonly DirectoryRow[]) {
  const { provided, snapshot } = useOrgDomain();
  return chatTargetInfo(target, directory, provided ? snapshot : undefined);
}
