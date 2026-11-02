import { useState } from 'react';
import type { AgentFilter } from '../../state/types';
import {
  AGENT_STATUS_LABELS,
  formatAgentCount,
  type ManagedAgentView,
} from '../../state/agent-selectors';
import {
  AGENT_RUNTIME_LABELS,
  runtimeTone,
  type AgentExtensionView,
} from './agent-extensions';
import { AgentAvatar } from '../../components/AgentAvatar';
import { Badge } from '../../components/Badge';
import { Button, Card, Select } from '../../components/primitives';
import { AgentOrgBadges } from '../org/crosstab/AgentOrgBadges';
import { ALL_ORGS, AgentOrgFilter, useAgentOrgFilter } from '../org/crosstab/AgentOrgFilter';

const FILTERS: readonly { readonly value: AgentFilter; readonly label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'available', label: '可用' },
  { value: 'needs-connection', label: '需要连接' },
  { value: 'disabled', label: '已停用' },
];

const AVAILABILITY_LABELS: Readonly<Record<ManagedAgentView['availability'], string>> = {
  available: '可用',
  'needs-connection': AGENT_STATUS_LABELS.needsModel,
  disabled: AGENT_STATUS_LABELS.disabled,
};

const OWN_GROUP = '我';

export interface AgentListProps {
  readonly unavailable?: boolean;
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
  readonly onCreate: () => void;
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
}: {
  readonly agent: ManagedAgentView;
  readonly extension?: AgentExtensionView;
}) {
  return (
    <>
      {extension?.preset && <span className="badge badge-preset">预配置</span>}
      {extension && (
        <Badge tone={runtimeTone(extension.runtime)}>
          {AGENT_RUNTIME_LABELS[extension.runtime]}
        </Badge>
      )}
      <span className="badge" data-availability={agent.availability}>
        {AVAILABILITY_LABELS[agent.availability]}
      </span>
    </>
  );
}

export function AgentList({
  unavailable = false,
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
  // dev-04：按组织筛选（组织数据就绪时才出现），清除筛选时一并清除。
  const [orgFilter, setOrgFilter] = useState(ALL_ORGS);
  const shown = useAgentOrgFilter(agents, orgFilter);
  const clearFilters = () => {
    setOrgFilter(ALL_ORGS);
    onClearFilters();
  };
  const hasFilters =
    query.trim().length > 0 ||
    filter !== 'all' ||
    ownerFilter !== '' ||
    hostFilter !== '' ||
    orgFilter !== ALL_ORGS;
  const isEmptyOrg = total === 0;
  const groups = groupByOwner(shown);

  return (
    <Card padding="lg" className="card contacts-card">
      <div className="section-heading contacts-heading">
        <div>
          <h2 ref={headingRef} tabIndex={-1}>
            Agent
          </h2>
          <p>{unavailable ? 'Agent 数量尚未确认' : isEmptyOrg ? '还没有 Agent 记录' : `${total} 个 Agent`}</p>
        </div>
        <Button
          variant="primary"
          className="button button-strong"
          disabled={unavailable} onClick={onCreate}
        >
          创建 Agent
        </Button>
      </div>

      {!isEmptyOrg && (
        <div className="contacts-tools">
          <label className="contacts-search">
            <span className="contacts-search-label">搜索 Agent</span>
            <span className="search-box">
              <span className="search-mark" aria-hidden="true">
                ⌕
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
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                className="filter-chip"
                aria-pressed={filter === option.value}
                onClick={() => onFilter(option.value)}
              >
                {option.label}
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
      {/* dev-04：组织筛选单独一行，不挤占上面一行的所有者、主机下拉。 */}
      {!isEmptyOrg && <AgentOrgFilter value={orgFilter} onChange={setOrgFilter} />}

      {!isEmptyOrg && (
        <p className="result-count" aria-live="polite">
          {unavailable ? 'Agent 列表状态未确认' : formatAgentCount(shown.length, total)}
        </p>
      )}

      {isEmptyOrg ? (
        <div className="empty-state contacts-empty">
          <strong>{unavailable ? 'Agent 管理状态尚未确认，创建和配置暂不可用。' : '还没有 Agent'}</strong>
          <p>{unavailable ? '连接服务后可读取列表。' : '创建第一个 Agent，从这里开始。'}</p>
          <Button
            variant="secondary"
            className="button button-secondary"
            disabled={unavailable} onClick={onCreate}
          >
            创建 Agent
          </Button>
        </div>
      ) : shown.length === 0 ? (
        <div className="empty-state contacts-empty">
          <strong>没有匹配的 Agent</strong>
          <p>换个关键词，或清除筛选后重试。</p>
          <button
            type="button"
            className="text-button"
            onClick={clearFilters}
          >
            清除筛选
          </button>
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
                          <AgentBadges agent={agent} extension={extension} />
                        </span>
                        <span className="contact-role">{agent.role}</span>
                        <span className="contact-badges">
                          <Badge tone="neutral">{ownerHostNote(agent)}</Badge>
                          <AgentOrgBadges
                            agentId={agent.id}
                            priorityOrgId={orgFilter || undefined}
                            fallback={
                              agent.organizationId && (
                                <Badge tone="neutral">{agent.organizationLabel}</Badge>
                              )
                            }
                          />
                          <Badge tone="neutral">{agent.channel.label}</Badge>
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
                      <span className="agent-row-role">{agent.role}</span>
                      <span className="agent-row-host">
                        {ownerHostNote(agent)}
                      </span>
                      <span className="agent-row-badges">
                        <AgentBadges agent={agent} extension={extension} />
                        <AgentOrgBadges
                          agentId={agent.id}
                          priorityOrgId={orgFilter || undefined}
                        />
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
