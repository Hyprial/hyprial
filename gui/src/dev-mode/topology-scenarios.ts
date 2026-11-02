import type { OrgMember } from '../pages/org/org-view-model';
import {
  MACHINE_CONNECTION_LABELS,
  type MachineRecord,
} from '../features/network/machine-view-model';
import {
  TOPOLOGY_COPY,
  buildUnavailableMonitor,
  type TopologyEdge,
  type TopologyModel,
  type TopologyNode,
} from '../features/network/topology-view-model';

/**
 * J7 development scenarios.
 *
 * Synthetic edges, Monitor samples and the observed timestamp live here, not in
 * `src/features/network/`, so the production page graph cannot ship them.
 * Nodes are derived from the J5 members and the J6 machines passed in.
 */
export const J7_SCENARIOS = [
  { value: 'baseline', label: '基线（无拓扑数据）' },
  { value: 'm16', label: 'M-16 通信数据不可用' },
  { value: 'm17', label: 'M-17 Inspect 有记录' },
] as const;

export type J7Scenario = (typeof J7_SCENARIOS)[number]['value'];

export function isJ7Scenario(value: string): value is J7Scenario {
  return J7_SCENARIOS.some((option) => option.value === value);
}

export const J7_EDGE: TopologyEdge = {
  id: 'edge-demo-1',
  fromId: 'member-node',
  toId: 'machine-node',
  status: 'completed',
};

export const J7_OBSERVED_AT = '2026-10-22 10:15';

export interface TopologyScenarioInput {
  readonly members: readonly OrgMember[];
  readonly machines: readonly MachineRecord[];
}

function buildNodes(input: TopologyScenarioInput): readonly TopologyNode[] {
  const memberNodes: TopologyNode[] = input.members.map((member) => ({
    id: `member-${member.id}`,
    kind: 'member',
    label: member.name,
    meta: '成员 · 已加入',
  }));
  const machineNodes: TopologyNode[] = input.machines.map((machine) => ({
    id: `machine-${machine.id}`,
    kind: 'machine',
    label: machine.hostname,
    meta: `${machine.system} · ${MACHINE_CONNECTION_LABELS[machine.connection]}`,
  }));
  return [...memberNodes, ...machineNodes];
}

function firstEdge(
  nodes: readonly TopologyNode[],
): TopologyEdge | undefined {
  const member = nodes.find((node) => node.kind === 'member');
  const machine = nodes.find((node) => node.kind === 'machine');
  if (!member || !machine) {
    return undefined;
  }
  return { ...J7_EDGE, fromId: member.id, toId: machine.id };
}

/** Baseline returns undefined so the page keeps its product default. */
export function buildTopologyScenario(
  scenario: J7Scenario,
  input: TopologyScenarioInput,
): TopologyModel | undefined {
  if (scenario === 'baseline') {
    return undefined;
  }

  const nodes = buildNodes(input);
  const edge = firstEdge(nodes);
  const base = {
    nodes,
    edges: [] as readonly TopologyEdge[],
    emptyMessage: null,
    unavailableMessage: null,
    monitor: buildUnavailableMonitor(),
  };

  if (scenario === 'm16') {
    return { ...base, dataMode: 'structure-only' };
  }

  const member = nodes.find((node) => node.kind === 'member');
  const machine = nodes.find((node) => node.kind === 'machine');

  return {
    ...base,
    dataMode: 'full',
    edges: edge ? [edge] : [],
    monitor: {
      mode: 'linked',
      title: '最近通信记录',
      sample: {
        fromLabel: member?.label ?? TOPOLOGY_COPY.unconfirmed,
        toLabel: machine?.label ?? TOPOLOGY_COPY.unconfirmed,
        status: 'completed',
        observedAt: J7_OBSERVED_AT,
      },
    },
  };
}
