import type { ConnectionSummary } from './gui-view-model';
import type { FixtureAgent } from '../fixtures/demo';
import type { AgentFilter, AppState } from './types';

/**
 * The single source of Agent state for the Agent page.
 *
 * No page may keep its own copy of a connection state: everything below is
 * derived from the seeded agents plus the connection list.
 */

export interface AgentSeedRecord {
  readonly id: string;
  readonly displayName: string;
  readonly role: string;
  readonly avatarTone: 'green' | 'lilac' | 'peach' | 'grey';
  readonly modelServiceId?: string;
  readonly channelServiceId?: string;
  readonly organizationId?: string | null;
  readonly modelLabel: string;
  readonly capabilities: readonly string[];
  /**
   * Owner label for network-reachable agents; agents without an owner are
   * the local operator's own (我). Host names the machine the agent runs on
   * (本机 when absent).
   */
  readonly owner?: string;
  readonly host?: string;
  /**
   * Present only for baseline fixtures that already declare a disabled Agent.
   * It keeps an unbound Agent that the product data marks as disabled from
   * being reported as merely unconnected.
   */
  readonly disabled?: boolean;
}

export interface AgentOrganization {
  readonly id: string;
  readonly label: string;
}

export interface AgentSeed {
  readonly agents: readonly AgentSeedRecord[];
  readonly connections: readonly ConnectionSummary[];
  readonly organizations: readonly AgentOrganization[];
}

export type AgentConnectionKind =
  | 'connected'
  | 'unconfigured'
  | 'unknown'
  | 'disabled';

export interface AgentConnectionView {
  readonly kind: AgentConnectionKind;
  readonly label: string;
  readonly sourceLabel: string;
}

export type AgentAvailability = 'available' | 'needs-connection' | 'disabled';

export interface ManagedAgentView {
  readonly id: string;
  readonly displayName: string;
  readonly role: string;
  readonly avatarTone: 'green' | 'lilac' | 'peach' | 'grey';
  readonly modelServiceId: string | null;
  readonly channelServiceId: string | null;
  readonly organizationId: string | null;
  readonly modelLabel: string;
  readonly capabilities: readonly string[];
  readonly organizationLabel: string;
  readonly ownerLabel: string;
  readonly host: string;
  readonly availability: AgentAvailability;
  readonly model: AgentConnectionView;
  readonly channel: AgentConnectionView;
}

export const AGENT_STATUS_LABELS = {
  connected: '已连接',
  needsModel: '需绑定模型凭据',
  unconfiguredChannel: '未连接渠道',
  unknown: '状态未确认',
  disabled: '已停用',
  noModel: '未指定模型',
} as const;

export const AGENT_NOT_FOUND = '找不到该 Agent';

const UI_BASE = 10;

export function formatAgentCount(visible: number, total: number): string {
  return `显示 ${visible} / ${total} 个 Agent`;
}

function pick(
  connectionId: string | null | undefined,
  connections: readonly ConnectionSummary[],
): ConnectionSummary | undefined {
  if (!connectionId) {
    return undefined;
  }
  return connections.find((connection) => connection.id === connectionId);
}

export function buildModelConnection(
  connectionId: string | null | undefined,
  connections: readonly ConnectionSummary[],
): AgentConnectionView {
  const connection = pick(connectionId, connections);
  if (!connection) {
    return {
      kind: 'unknown',
      label: AGENT_STATUS_LABELS.unknown,
      sourceLabel: AGENT_STATUS_LABELS.unknown,
    };
  }
  if (connection.status === 'connected') {
    return {
      kind: 'connected',
      label: AGENT_STATUS_LABELS.connected,
      sourceLabel: connection.label,
    };
  }
  if (connection.status === 'disabled') {
    return {
      kind: 'disabled',
      label: AGENT_STATUS_LABELS.disabled,
      sourceLabel: connection.label,
    };
  }
  return {
    kind: 'unconfigured',
    label: AGENT_STATUS_LABELS.needsModel,
    sourceLabel: connection.label,
  };
}

export function buildChannelConnection(
  connectionId: string | null | undefined,
  connections: readonly ConnectionSummary[],
): AgentConnectionView {
  const connection = pick(connectionId, connections);
  if (!connection) {
    return {
      kind: 'unknown',
      label: AGENT_STATUS_LABELS.unknown,
      sourceLabel: AGENT_STATUS_LABELS.unknown,
    };
  }
  if (connection.status === 'connected') {
    return {
      kind: 'connected',
      label: AGENT_STATUS_LABELS.connected,
      sourceLabel: connection.label,
    };
  }
  if (connection.status === 'disabled') {
    return {
      kind: 'disabled',
      label: AGENT_STATUS_LABELS.disabled,
      sourceLabel: connection.label,
    };
  }
  return {
    kind: 'unconfigured',
    label: AGENT_STATUS_LABELS.unconfiguredChannel,
    sourceLabel: connection.label,
  };
}

function availabilityFrom(
  model: AgentConnectionView,
  disabledHint?: boolean,
): AgentAvailability {
  if (model.kind === 'connected') {
    return 'available';
  }
  if (model.kind === 'disabled') {
    return 'disabled';
  }
  return disabledHint ? 'disabled' : 'needs-connection';
}

function organizationLabel(
  organizationId: string | null,
  organizations: readonly AgentOrganization[],
): string {
  if (!organizationId) {
    return '未加入组织';
  }
  return (
    organizations.find((organization) => organization.id === organizationId)
      ?.label ?? '组织状态未确认'
  );
}

export function buildManagedAgent(
  record: AgentSeedRecord,
  connections: readonly ConnectionSummary[],
  organizations: readonly AgentOrganization[],
): ManagedAgentView {
  const model = buildModelConnection(record.modelServiceId, connections);
  const channel = buildChannelConnection(record.channelServiceId, connections);
  return {
    id: record.id,
    displayName: record.displayName,
    role: record.role,
    avatarTone: record.avatarTone,
    modelServiceId: record.modelServiceId ?? null,
    channelServiceId: record.channelServiceId ?? null,
    organizationId: record.organizationId ?? null,
    modelLabel: record.modelServiceId
      ? record.modelLabel
      : AGENT_STATUS_LABELS.noModel,
    capabilities: [...record.capabilities],
    organizationLabel: organizationLabel(
      record.organizationId ?? null,
      organizations,
    ),
    ownerLabel: record.owner ?? '我',
    host: record.host ?? '本机',
    availability: availabilityFrom(model, record.disabled),
    model,
    channel,
  };
}

function recordFromFixture(agent: FixtureAgent): AgentSeedRecord {
  return {
    id: agent.id,
    displayName: agent.displayName,
    role: agent.role,
    avatarTone: agent.avatarTone,
    modelServiceId: agent.modelServiceId,
    modelLabel: agent.modelLabel,
    capabilities: agent.capabilities,
    disabled: agent.availability === 'disabled',
  };
}

/** Baseline agents come from the product fixtures; a seed replaces them. */
export function selectManagedAgents(
  state: AppState,
  seed?: AgentSeed,
): readonly ManagedAgentView[] {
  const connections = seed?.connections ?? state.connections;
  const organizations = seed?.organizations ?? [];
  const records: readonly AgentSeedRecord[] = seed
    ? seed.agents
    : state.targets.map(recordFromFixture);
  return records.map((record) =>
    buildManagedAgent(record, connections, organizations),
  );
}

export function getManagedAgent(
  agents: readonly ManagedAgentView[],
  agentId: string,
): ManagedAgentView | undefined {
  return agents.find((agent) => agent.id === agentId);
}

export function filterManagedAgents(
  agents: readonly ManagedAgentView[],
  query: string,
  filter: AgentFilter,
): readonly ManagedAgentView[] {
  const needle = query.trim().toLowerCase();
  return agents.filter((agent) => {
    if (filter !== 'all' && agent.availability !== filter) {
      return false;
    }
    if (!needle) {
      return true;
    }
    return [
      agent.displayName,
      agent.role,
      agent.modelLabel,
      agent.ownerLabel,
      agent.host,
      ...agent.capabilities,
    ]
      .join(' ')
      .toLowerCase()
      .includes(needle);
  });
}

/** Only a connected model service may be chosen for an Agent. */
export function getConnectedModelOptions(
  connections: readonly ConnectionSummary[],
): readonly ConnectionSummary[] {
  return connections.filter(
    (connection) => connection.kind === 'model' && connection.status === 'connected',
  );
}

export function nextAvatarTone(index: number): AgentSeedRecord['avatarTone'] {
  const tones: readonly AgentSeedRecord['avatarTone'][] = [
    'green',
    'lilac',
    'peach',
    'grey',
  ];
  return tones[index % tones.length];
}

export function nextAgentId(
  existing: readonly ManagedAgentView[],
  prefix = 'agent-created',
): string {
  let index = existing.length + 1;
  let candidate = `${prefix}-${index}`;
  while (existing.some((agent) => agent.id === candidate)) {
    index += UI_BASE;
    candidate = `${prefix}-${index}`;
  }
  return candidate;
}
