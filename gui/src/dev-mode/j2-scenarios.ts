import {
  buildManagedAgent,
  type AgentSeed,
} from '../state/agent-selectors';
import {
  createLocalAgentGateway,
  type AgentMutationGateway,
} from '../features/agents/AgentMutationGateway';
import type { ConnectionSummary } from '../state/gui-view-model';

/**
 * J2 development scenarios.
 *
 * Only the four R4 mock scenes are selectable. Create success is a local
 * interaction inside M-04 and M-05; failure and unknown results have no scene
 * number and are injected by tests instead.
 */
export const J2_SCENARIOS = [
  { value: 'baseline', label: '基线（无 Agent 数据）' },
  { value: 'm04', label: 'M-04 Agent 列表有数据' },
  { value: 'm05', label: 'M-05 Agent 列表为空' },
  { value: 'm06', label: 'M-06 Agent 需连接模型' },
] as const;

export type J2Scenario = (typeof J2_SCENARIOS)[number]['value'];

export function isJ2Scenario(value: string): value is J2Scenario {
  return J2_SCENARIOS.some((option) => option.value === value);
}

const CONNECTIONS: readonly ConnectionSummary[] = [
  {
    id: 'model-connected',
    kind: 'model',
    label: '主模型服务',
    detail: '已连接',
    status: 'connected',
    usedBy: 3,
  },
  {
    id: 'model-open',
    kind: 'model',
    label: '备用模型服务',
    detail: '尚未配置',
    status: 'unconfigured',
    usedBy: 0,
  },
  {
    id: 'channel-connected',
    kind: 'adapter',
    label: '飞书连接',
    detail: '已连接',
    status: 'connected',
    usedBy: 1,
  },
  {
    id: 'channel-open',
    kind: 'adapter',
    label: '消息连接',
    detail: '未配置',
    status: 'unconfigured',
    usedBy: 0,
  },
];

const ORGANIZATIONS = [
  { id: 'org-design', label: '设计组' },
  { id: 'org-ops', label: '运营组' },
];

const M04_SEED: AgentSeed = {
  connections: CONNECTIONS,
  organizations: ORGANIZATIONS,
  agents: [
    {
      id: 'agent-squire',
      displayName: 'Squire',
      role: '个人助理 · 统一入口',
      avatarTone: 'green',
      modelServiceId: 'model-connected',
      channelServiceId: 'channel-connected',
      organizationId: 'org-design',
      modelLabel: '主模型服务',
      capabilities: ['日程整理', '消息归档'],
    },
    {
      id: 'agent-researcher',
      displayName: '研究者',
      role: '研究分析',
      avatarTone: 'lilac',
      modelServiceId: 'model-connected',
      channelServiceId: 'channel-open',
      organizationId: 'org-design',
      modelLabel: '主模型服务',
      capabilities: ['资料检索', '对比分析'],
    },
    {
      id: 'agent-writer',
      displayName: '写作者',
      role: '内容写作',
      avatarTone: 'peach',
      modelServiceId: 'model-connected',
      organizationId: 'org-ops',
      modelLabel: '主模型服务',
      capabilities: ['长文撰写'],
    },
  ],
};

const M05_SEED: AgentSeed = {
  connections: CONNECTIONS,
  organizations: ORGANIZATIONS,
  agents: [],
};

/** M-06 has no connected model service, so creation stays unavailable. */
const M06_CONNECTIONS: readonly ConnectionSummary[] = [
  {
    id: 'model-open',
    kind: 'model',
    label: '备用模型服务',
    detail: '尚未配置',
    status: 'unconfigured',
    usedBy: 0,
  },
  {
    id: 'channel-open',
    kind: 'adapter',
    label: '消息连接',
    detail: '未配置',
    status: 'unconfigured',
    usedBy: 0,
  },
];

const M06_SEED: AgentSeed = {
  connections: M06_CONNECTIONS,
  organizations: ORGANIZATIONS,
  agents: [
    {
      id: 'agent-unconnected',
      displayName: '待连接 Agent',
      role: '草稿',
      avatarTone: 'grey',
      modelServiceId: 'model-open',
      channelServiceId: 'channel-open',
      organizationId: null,
      modelLabel: '备用模型服务',
      capabilities: [],
    },
  ],
};

export interface AgentScenario {
  readonly seed: AgentSeed;
  readonly gateway: AgentMutationGateway;
}

export function buildAgentScenario(
  scenario: J2Scenario,
): AgentScenario | undefined {
  if (scenario === 'baseline') {
    return undefined;
  }
  const seed =
    scenario === 'm04' ? M04_SEED : scenario === 'm05' ? M05_SEED : M06_SEED;
  return {
    seed,
    gateway: createLocalAgentGateway({
      agents: seed.agents.map((record) =>
        buildManagedAgent(record, seed.connections, seed.organizations),
      ),
      connections: seed.connections,
      organizations: seed.organizations,
    }),
  };
}
