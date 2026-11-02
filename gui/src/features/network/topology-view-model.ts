/**
 * J7 topology view model.
 *
 * The product module only holds types, fixed product copy and the local
 * unavailable/hidden/unconfirmed states. Synthetic members, machines, edges,
 * Inspect and Monitor samples belong to the integrator's dev-mode scenarios.
 */

export type TopologyDataMode =
  | 'unavailable'
  | 'hidden'
  | 'structure-only'
  | 'full'
  | 'empty'
  | 'unconfirmed';

export type TopologyView = 'structure' | 'communication';

export type TopologyNodeKind = 'member' | 'machine';

export type CommunicationStatus = 'in_flight' | 'completed';

export interface TopologyNode {
  readonly id: string;
  readonly kind: TopologyNodeKind;
  readonly label: string;
  readonly meta: string;
}

export interface TopologyEdge {
  readonly id: string;
  readonly fromId: string;
  readonly toId: string;
  readonly status: CommunicationStatus;
}

export interface MonitorModel {
  readonly mode: 'unavailable' | 'linked';
  readonly title: string;
  readonly sample: {
    readonly fromLabel: string;
    readonly toLabel: string;
    readonly status: CommunicationStatus;
    readonly observedAt: string;
  } | null;
}

export interface TopologyModel {
  readonly dataMode: TopologyDataMode;
  readonly nodes: readonly TopologyNode[];
  readonly edges: readonly TopologyEdge[];
  readonly emptyMessage: string | null;
  readonly unavailableMessage: string | null;
  readonly monitor: MonitorModel;
}

export const TOPOLOGY_COPY = {
  title: '组织拓扑',
  structureView: '结构视图',
  communicationView: '通信视图',
  unavailable: '拓扑数据暂不可用',
  communicationUnavailable: '通信数据暂不可用',
  monitorUnavailable: 'Monitor 暂不可用',
  monitorTitle: '最近通信记录',
  unconfirmed: '状态未确认',
  retry: '重试',
  emptyHint: '邀请成员后这里会出现更多节点',
  legend: {
    member: '圆形：成员',
    machine: '圆角矩形：机器',
    edge: '箭头：通信方向',
  },
} as const;

export const COMMUNICATION_STATUS_LABELS: Readonly<
  Record<CommunicationStatus, string>
> = {
  in_flight: '传输中',
  completed: '已完成',
};

const BASE: Omit<TopologyModel, 'dataMode'> = {
  nodes: [],
  edges: [],
  emptyMessage: null,
  unavailableMessage: null,
  monitor: {
    mode: 'unavailable',
    title: TOPOLOGY_COPY.monitorTitle,
    sample: null,
  },
};

export function buildUnavailableMonitor(): MonitorModel {
  return {
    mode: 'unavailable',
    title: TOPOLOGY_COPY.monitorTitle,
    sample: null,
  };
}

export function buildUnavailableTopologyModel(): TopologyModel {
  return {
    ...BASE,
    dataMode: 'unavailable',
    unavailableMessage: TOPOLOGY_COPY.unavailable,
  };
}

export function buildHiddenTopologyModel(): TopologyModel {
  return { ...BASE, dataMode: 'hidden' };
}

export function buildUnconfirmedTopologyModel(): TopologyModel {
  return {
    ...BASE,
    dataMode: 'unconfirmed',
    unavailableMessage: TOPOLOGY_COPY.unconfirmed,
  };
}
