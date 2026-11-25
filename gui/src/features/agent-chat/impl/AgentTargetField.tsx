import { useState, type ReactNode } from 'react';
import { AgentAvatar } from '../../../components/AgentAvatar';
import { AvailabilityBadge } from '../../../components/Badge';
import { Button, EmptyState, ErrorState, Icon, LoadingState } from '../../../components/primitives';
import type { SessionTarget } from '../../../state/gui-view-model';
import { orgDisplayName, useOrgDomain } from '../../org';
import { targetRoleLabel } from './target-presentation';

/** Controlled projection of the targets.list directory. */
export function AgentTargetField({
  targets,
  groups = [],
  groupsUnknown = false,
  highlightedGroupId,
  onRefreshGroups,
  groupActions,
  localAgentUris,
  activeTargetId,
  onValueChange,
  state = 'ready',
  error,
  disabled = false,
  onRetry,
}: {
  readonly targets: readonly SessionTarget[];
  readonly groups?: readonly SessionTarget[];
  readonly groupsUnknown?: boolean;
  readonly highlightedGroupId?: string | null;
  readonly onRefreshGroups?: () => void;
  readonly groupActions?: ReactNode;
  readonly localAgentUris?: readonly string[];
  readonly activeTargetId: string | null;
  readonly onValueChange: (id: string) => void;
  readonly state?: 'loading' | 'ready' | 'error';
  readonly error?: string;
  readonly disabled?: boolean;
  readonly onRetry?: () => void;
}) {
  const { provided, snapshot } = useOrgDomain();
  const [groupsCollapsed, setGroupsCollapsed] = useState(false);
  const [localCollapsed, setLocalCollapsed] = useState(false);
  const [collapsedOrgId, setCollapsedOrgId] = useState<string | null>(null);
  const orgReady = provided && snapshot.status === 'ready';
  const currentOrgId = orgReady ? snapshot.currentOrgId : null;
  const currentOrg = orgReady ? snapshot.orgs?.find((org) => org.id === currentOrgId) : null;
  const organizationName = currentOrg ? orgDisplayName(currentOrg) : null;
  const localUris = new Set(localAgentUris ?? []);
  const localFixtureIds = new Set(
    orgReady && snapshot.thisComputer
      ? Object.values(snapshot.details).flatMap((detail) =>
        (detail.agents ?? [])
          .filter((agent) => agent.machineLabel === snapshot.thisComputer?.hostname)
          .flatMap((agent) => [agent.id, ...(agent.chatTargetId ? [agent.chatTargetId] : [])]),
      )
      : [],
  );
  const localTargets: SessionTarget[] = [];
  const organizationTargets: SessionTarget[] = [];
  const otherTargets: SessionTarget[] = [];
  const orgNames = new Map<string, readonly string[]>();
  for (const target of targets) {
    const key = target.agentId ?? target.id;
    const memberships = orgReady ? snapshot.agentOrgs?.[key] ?? [] : [];
    orgNames.set(target.id, memberships.flatMap((id) => {
      const org = snapshot.orgs?.find((candidate) => candidate.id === id);
      return org ? [orgDisplayName(org)] : [];
    }));
    if (localUris.has(target.id) || localFixtureIds.has(key) || localFixtureIds.has(target.id)) {
      localTargets.push(target);
    } else if (currentOrgId && memberships.includes(currentOrgId)) {
      organizationTargets.push(target);
    } else {
      otherTargets.push(target);
    }
  }
  const targetButton = (target: SessionTarget) => {
    const roleLabel = targetRoleLabel(target);
    return <button key={target.id} data-target-id={target.id} data-highlighted={target.id === highlightedGroupId ? 'true' : undefined} type="button" className="chat-workspace__target"
      title={target.id} aria-pressed={target.id === activeTargetId} disabled={disabled} onClick={() => onValueChange(target.id)}>
      <AgentAvatar name={target.displayName} tone={target.avatarTone} />
      <span className="chat-workspace__target-copy"><strong>{target.displayName}</strong>
        {roleLabel && <span className="chat-workspace__target-role">{roleLabel}</span>}
        {target.kind !== 'group' && <AvailabilityBadge availability={target.availability} />}
        {(orgNames.get(target.id)?.length ?? 0) > 0 && <small className="chat-workspace__target-orgs">{orgNames.get(target.id)?.join(' · ')}</small>}
      </span>
    </button>;
  };
  const hasGroupsSection = groups.length > 0 || groupsUnknown || groupActions !== undefined;
  const organizationLabel = `当前组织 · ${organizationName ?? '状态未确认'}`;
  const organizationCollapsed = collapsedOrgId === currentOrgId;
  return <aside className="chat-workspace__targets" aria-label="聊天对象">
    <h2>聊天对象</h2>
    {state === 'loading' && targets.length === 0 && !hasGroupsSection ? <LoadingState title="正在加载聊天对象" /> :
      state === 'error' && targets.length === 0 && !hasGroupsSection ? <ErrorState message={error ?? '聊天对象目录不可用。'}
        retryable={onRetry !== undefined} onRetry={onRetry} /> :
      targets.length === 0 && !hasGroupsSection ? <EmptyState title="暂无聊天对象" /> :
      <div className="chat-workspace__target-list">
        {state === 'error' && <ErrorState message={error ?? '聊天对象目录不可用。'}
          retryable={onRetry !== undefined} onRetry={onRetry} />}
        <>
          {hasGroupsSection && <section className="chat-workspace__target-group" role="group" aria-label="群聊">
            <div className="chat-workspace__target-group-header">
              <button type="button" className="chat-workspace__target-group-toggle disclosure-trigger" aria-expanded={!groupsCollapsed}
                aria-label={`${groupsCollapsed ? '展开' : '收起'}群聊`}
                onClick={() => setGroupsCollapsed((value) => !value)}>
                <span>群聊 {groups.length > 0 && <span>{groups.length}</span>}</span>
                <span aria-hidden="true">{groupsCollapsed ? '▸' : '▾'}</span>
              </button>
              {(groupActions || onRefreshGroups) && <div className="chat-workspace__target-group-actions">
                {groupActions}
                {onRefreshGroups && <Button variant="ghost" size="sm" aria-label="刷新列表" title="刷新列表"
                  onClick={onRefreshGroups}><Icon name="arrow-clockwise" size="sm" /></Button>}
              </div>}
            </div>
            {!groupsCollapsed && <>
              {groupsUnknown && <p className="chat-workspace__group-unknown" role="status" data-inline-feedback="state">群聊列表状态未确认。</p>}
              {groups.length > 0 && <div className="chat-workspace__target-group-items">{groups.map(targetButton)}</div>}
            </>}
          </section>}
          {localTargets.length > 0 && <section className="chat-workspace__target-group" role="group" aria-label="本机 Agent">
            <button type="button" className="chat-workspace__target-group-toggle disclosure-trigger" aria-expanded={!localCollapsed}
              aria-label={`${localCollapsed ? '展开' : '收起'}本机 Agent`}
              onClick={() => setLocalCollapsed((value) => !value)}>
              <span>本机 Agent <span>{localTargets.length}</span></span>
              <span aria-hidden="true">{localCollapsed ? '▸' : '▾'}</span>
            </button>
            {!localCollapsed && <div className="chat-workspace__target-group-items">{localTargets.map(targetButton)}</div>}
          </section>}
          {currentOrgId && organizationTargets.length > 0 && <section className="chat-workspace__target-group" role="group" aria-label={organizationLabel}>
            <button type="button" className="chat-workspace__target-group-toggle disclosure-trigger" aria-expanded={!organizationCollapsed}
              aria-label={`${organizationCollapsed ? '展开' : '收起'}当前组织 ${organizationName ?? ''}`}
              onClick={() => setCollapsedOrgId(organizationCollapsed ? null : currentOrgId)}>
              <span>{organizationLabel} <span>{organizationTargets.length}</span></span>
              <span aria-hidden="true">{organizationCollapsed ? '▸' : '▾'}</span>
            </button>
            {!organizationCollapsed && <div className="chat-workspace__target-group-items">{organizationTargets.map(targetButton)}</div>}
          </section>}
          {otherTargets.length > 0 && <section className="chat-workspace__target-group" role="group" aria-label="其他可联系">
            <h3>其他可联系 <span>{otherTargets.length}</span></h3>
            <div className="chat-workspace__target-group-items">{otherTargets.map(targetButton)}</div>
          </section>}
        </>
      </div>}
  </aside>;
}
