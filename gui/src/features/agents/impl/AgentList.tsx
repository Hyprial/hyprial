import { useState } from 'react';
import type { AgentFilter } from '../../../state/types';
import {
  AGENT_STATUS_LABELS,
  formatAgentCount,
  type ManagedAgentView,
} from '../../../state/agent-selectors';
import {
  AGENT_RUNTIME_LABELS,
  runtimeTone,
  type AgentExtensionView,
} from './agent-extensions';
import { AgentAvatar } from '../../../components/AgentAvatar';
import { Badge } from '../../../components/Badge';
import { Button, Card, Icon, Select } from '../../../components/primitives';
import { AgentOrgBadges, ALL_ORGS, AgentOrgFilter, useAgentOrgFilter, useOrgDomain } from '../../org';

const FILTERS: readonly AgentFilter[] = [
  'all',
  'available',
  'needs-connection',
  'disabled',
];

type AvailabilityMode = 'connection' | 'runtime';

function filtersFor(mode: AvailabilityMode): readonly AgentFilter[] {
  return mode === 'runtime'
    ? [...FILTERS.filter((filter) => filter !== 'disabled'), 'unknown']
    : FILTERS;
}

function filterLabel(filter: AgentFilter, mode: AvailabilityMode): string {
  switch (filter) {
    case 'all':
      return '全部';
    case 'available':
      return mode === 'runtime' ? '运行中' : '可用';
    case 'needs-connection':
      return mode === 'runtime' ? '未运行' : '需要连接';
    case 'disabled':
      return '已停用';
    case 'unknown':
      return '状态未确认';
  }
}

function availabilityLabel(
  availability: ManagedAgentView['availability'],
  mode: AvailabilityMode,
): string {
  if (availability === 'available') {
    return mode === 'runtime' ? '运行中' : '可用';
  }
  if (availability === 'needs-connection') {
    return mode === 'runtime' ? '未运行' : AGENT_STATUS_LABELS.needsModel;
  }
  return availability === 'disabled'
    ? AGENT_STATUS_LABELS.disabled
    : AGENT_STATUS_LABELS.unknown;
}

const OWN_GROUP = '我';

export interface AgentListProps {
  /** 目录数据用运行态标签；fixture 用连接可用性标签。 */
  readonly availabilityMode?: AvailabilityMode;
  readonly agents: readonly ManagedAgentView[];
  readonly total: number;
  readonly query: string;
  readonly filter: AgentFilter;
  readonly extensions?: Readonly<Record<string, AgentExtensionView>>;
  readonly ownerFilter: string;
  readonly hostFilter: string;
  readonly ownerOptions: readonly string[];
  readonly hostOptions: readonly string[];
  readonly onOwnerFilter: (owner: string) => void;
  readonly onHostFilter: (host: string) => void;
  /** Absent when the composition has no way to create an Agent: no control is rendered. */
  readonly onCreate?: () => void;
  readonly onOpen: (agentId: string) => void;
  readonly onQuery: (value: string) => void;
  readonly onFilter: (filter: AgentFilter) => void;
  readonly onClearFilters: () => void;
  readonly headingRef?: React.Ref<HTMLHeadingElement>;
}

interface AgentGroup {
  readonly owner: string;
  readonly title: string;
  readonly agents: readonly ManagedAgentView[];
}

function groupByOwner(
  agents: readonly ManagedAgentView[],
): readonly AgentGroup[] {
  const groups = new Map<string, ManagedAgentView[]>();
  for (const agent of agents) {
    const bucket = groups.get(agent.ownerLabel) ?? [];
    bucket.push(agent);
    groups.set(agent.ownerLabel, bucket);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === OWN_GROUP ? -1 : b === OWN_GROUP ? 1 : a.localeCompare(b, 'zh')))
    .map(([owner, bucket]) => ({
      owner,
      title: owner === OWN_GROUP ? '我的' : owner,
      agents: bucket,
    }));
}

function ownerHostNote(agent: ManagedAgentView): string {
  return agent.ownerLabel === OWN_GROUP
    ? agent.host
    : `${agent.ownerLabel} · ${agent.host}`;
}

function AgentBadges({
  agent,
  extension,
  availabilityMode,
}: {
  readonly agent: ManagedAgentView;
  readonly extension?: AgentExtensionView;
  readonly availabilityMode: AvailabilityMode;
}) {
  return (
    <>
      {extension?.preset && <span className="badge badge-preset">预配置</span>}
      {availabilityMode === 'runtime' ? (
        <span className="badge" data-availability={agent.availability}>
          {availabilityLabel(agent.availability, availabilityMode)}
        </span>
      ) : (
        <>
          {extension && extension.runtime !== 'unknown' && (
            <Badge tone={runtimeTone(extension.runtime)}>
              {AGENT_RUNTIME_LABELS[extension.runtime]}
            </Badge>
          )}
          <span className="badge" data-availability={agent.availability}>
            {availabilityLabel(agent.availability, availabilityMode)}
          </span>
        </>
      )}
    </>
  );
}

export function AgentList({
  availabilityMode = 'connection',
  agents,
  total,
  query,
  filter,
  extensions,
  ownerFilter,
  hostFilter,
  ownerOptions,
  hostOptions,
  onOwnerFilter,
  onHostFilter,
  onCreate,
  onOpen,
  onQuery,
  onFilter,
  onClearFilters,
  headingRef,
}: AgentListProps) {
  const [view, setView] = useState<'cards' | 'list'>('cards');
  const { provided: orgProvided, snapshot: orgSnapshot } = useOrgDomain();
  const currentOrgId = orgProvided && (orgSnapshot.status === 'ready' || orgSnapshot.orgAgentDirectory !== undefined)
    ? orgSnapshot.currentOrgId
    : null;
  const [orgSelection, setOrgSelection] = useState<{ readonly current: string | null; readonly value: string } | null>(null);
  const orgFilter = orgSelection?.current === currentOrgId
    ? orgSelection.value
    : orgSnapshot.orgAgentDirectory !== undefined ? currentOrgId ?? ALL_ORGS : ALL_ORGS;
  const hasOrgData = orgProvided && orgSnapshot.status === 'ready' &&
    (orgSnapshot.agentOrgs !== null || orgSnapshot.orgAgentDirectory !== undefined);
  const orgUnavailable = orgFilter !== ALL_ORGS && orgSnapshot.orgAgentDirectory?.[orgFilter] === null;
  const partialOrgDirectory = orgSnapshot.orgAgentDirectory !== undefined && (
    orgFilter === ALL_ORGS
      ? Object.values(orgSnapshot.orgAgentDirectoryStatus ?? {}).some((status) => status !== 'ok') ||
        Object.values(orgSnapshot.orgAgentDirectory).some((records) => records === null)
      : orgSnapshot.orgAgentDirectoryStatus?.[orgFilter] === 'partial'
  );
  const shown = useAgentOrgFilter(agents, orgFilter);
  const clearFilters = () => {
    setOrgSelection({ current: currentOrgId, value: ALL_ORGS });
    onClearFilters();
  };
  const hasFilters =
    query.trim().length > 0 ||
    ownerFilter !== '' ||
    hostFilter !== '' ||
    (orgSelection?.current === currentOrgId && orgFilter !== ALL_ORGS);
  const isEmptyOrg = total === 0;
  const emptyCurrentOrg = shown.length === 0 && !hasFilters && orgFilter !== ALL_ORGS;
  const groups = groupByOwner(shown);

  return (
    <Card padding="lg" className="card contacts-card">
      <div className="section-heading contacts-heading">
        <div>
          <h2 ref={headingRef} tabIndex={-1} className="sr-only">
            Agent 列表
          </h2>
        </div>
        {onCreate && (
          <Button
            variant="primary"
            leadingIcon={<Icon name="plus" />}
            onClick={onCreate}
          >
            创建 Agent
          </Button>
        )}
      </div>

      {!isEmptyOrg && (
        <div className="contacts-tools">
          <label className="contacts-search">
            <span className="sr-only">搜索 Agent</span>
            <span className="search-box">
              <span className="search-mark" aria-hidden="true">
                <Icon name="magnifying-glass" />
              </span>
              <input
                type="search"
                value={query}
                placeholder="按名称、角色、模型、所有者或主机搜索"
                onChange={(event) => onQuery(event.target.value)}
              />
            </span>
          </label>

          <div className="filter-row" role="group" aria-label="Agent 筛选">
            {filtersFor(availabilityMode).map((option) => (
              <button
                key={option}
                type="button"
                className="filter-chip"
                aria-pressed={filter === option}
                onClick={() => onFilter(option)}
              >
                {filterLabel(option, availabilityMode)}
              </button>
            ))}
            {hasFilters && (
              <button
                type="button"
                className="filter-chip filter-clear"
                onClick={clearFilters}
              >
                清除筛选
              </button>
            )}
          </div>

          <div className="contacts-dimensions">
            <label htmlFor="agent-owner-filter">所有者</label>
            <Select
              id="agent-owner-filter"
              className="contacts-dimension-select"
              value={ownerFilter}
              options={[
                { value: '', label: '全部' },
                ...ownerOptions.map((owner) => ({ value: owner, label: owner })),
              ]}
              onValueChange={onOwnerFilter}
            />
            <label htmlFor="agent-host-filter">主机</label>
            <Select
              id="agent-host-filter"
              className="contacts-dimension-select"
              value={hostFilter}
              options={[
                { value: '', label: '全部' },
                ...hostOptions.map((host) => ({ value: host, label: host })),
              ]}
              onValueChange={onHostFilter}
            />
            <div className="view-toggle" role="group" aria-label="视图">
              <button
                type="button"
                className="filter-chip"
                aria-pressed={view === 'cards'}
                onClick={() => setView('cards')}
              >
                卡片
              </button>
              <button
                type="button"
                className="filter-chip"
                aria-pressed={view === 'list'}
                onClick={() => setView('list')}
              >
                列表
              </button>
            </div>
          </div>
        </div>
      )}
      {!isEmptyOrg && hasOrgData && <AgentOrgFilter value={orgFilter} onChange={(value) => setOrgSelection({ current: currentOrgId, value })} />}
      {partialOrgDirectory && <p role="alert">部分组织 Agent 列表未能确认最新状态，可能显示旧数据</p>}
      {!isEmptyOrg && hasFilters && (
        <p className="result-count" aria-live="polite">
          {formatAgentCount(shown.length, total)}
        </p>
      )}

      {orgUnavailable ? (
        <div className="empty-state contacts-empty" role="alert">
          <strong>组织 Agent 列表尚未同步完整</strong>
        </div>
      ) : isEmptyOrg ? (
        <div className="empty-state contacts-empty">
          <strong>{onCreate ? '还没有 Agent。使用上方“创建 Agent”开始。' : '还没有 Agent。'}</strong>
        </div>
      ) : shown.length === 0 ? (
        <div className="empty-state contacts-empty">
          <strong>{emptyCurrentOrg ? '当前组织还没有 Agent' : '没有匹配的 Agent'}</strong>
          {hasFilters && <Button
            variant="text"
            size="compact"
            onClick={() => clearFilters()}
          >
            清除筛选
          </Button>}
        </div>
      ) : view === 'cards' ? (
        <div data-testid="contact-list" className="agent-groups">
          {groups.map((group) => (
            <section
              key={group.owner}
              className="agent-group"
              aria-label={group.title}
            >
              <h3 className="agent-group-title">
                {group.title}
                <span className="agent-group-count">{group.agents.length}</span>
              </h3>
              <div className="agent-list">
                {group.agents.map((agent) => {
                  const extension = extensions?.[agent.id];
                  return (
                    <button
                      key={agent.id}
                      type="button"
                      className="contact-card"
                      data-testid={`contact-${agent.id}`}
                      title={`打开 ${agent.displayName} 的详情`}
                      onClick={() => onOpen(agent.id)}
                    >
                      <AgentAvatar
                        name={agent.displayName}
                        tone={agent.avatarTone}
                        size="lg"
                      />
                      <span className="contact-main">
                        <span className="contact-name-row">
                          <strong>{agent.displayName}</strong>
                          <AgentBadges
                            agent={agent}
                            extension={extension}
                            availabilityMode={availabilityMode}
                          />
                        </span>
                        <span className="contact-role">
                          {agent.role || agent.modelLabel}
                        </span>
                        <span className="contact-badges">
                          <Badge tone="neutral">{ownerHostNote(agent)}</Badge>
                          {hasOrgData && (
                            <AgentOrgBadges
                              agentId={agent.agentId ?? agent.id}
                              priorityOrgId={orgFilter || undefined}
                              fallback={
                                agent.organizationId && (
                                  <Badge tone="neutral">{agent.organizationLabel}</Badge>
                                )
                              }
                            />
                          )}
                          {(availabilityMode === 'connection' ||
                            agent.channel.kind !== 'unknown') && (
                            <Badge tone="neutral">{agent.channel.label}</Badge>
                          )}
                        </span>
                        <span className="contact-capabilities">
                          {agent.capabilities.map((capability) => (
                            <span key={capability} className="capability-chip">
                              {capability}
                            </span>
                          ))}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div data-testid="contact-list" className="agent-groups">
          {groups.map((group) => (
            <section
              key={group.owner}
              className="agent-group"
              aria-label={group.title}
            >
              <h3 className="agent-group-title">
                {group.title}
                <span className="agent-group-count">{group.agents.length}</span>
              </h3>
              <div className="agent-rows">
                {group.agents.map((agent) => {
                  const extension = extensions?.[agent.id];
                  return (
                    <button
                      key={agent.id}
                      type="button"
                      className="agent-row"
                      data-testid={`contact-${agent.id}`}
                      onClick={() => onOpen(agent.id)}
                    >
                      <AgentAvatar
                        name={agent.displayName}
                        tone={agent.avatarTone}
                        size="sm"
                      />
                      <span className="agent-row-name">{agent.displayName}</span>
                      <span className="agent-row-role">
                        {agent.role || agent.modelLabel}
                      </span>
                      <span className="agent-row-host">
                        {ownerHostNote(agent)}
                      </span>
                      <span className="agent-row-badges">
                        <AgentBadges
                          agent={agent}
                          extension={extension}
                          availabilityMode={availabilityMode}
                        />
                        {hasOrgData && (
                          <AgentOrgBadges
                            agentId={agent.agentId ?? agent.id}
                            priorityOrgId={orgFilter || undefined}
                          />
                        )}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </Card>
  );
}
