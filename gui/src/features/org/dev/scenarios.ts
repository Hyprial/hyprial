/**
 * 组织开发情形（交互设计第 14 节）。开发定位层与 dev-04 验收页选择其中一项，
 * 把数据源与配套的 Agent、聊天开发数据一起交给 App。产品入口不引用本文件。
 */
import {
  buildManagedAgent,
  type AgentSeed,
  type AgentSeedRecord,
} from '../../../state/agent-selectors';
import {
  createLocalAgentGateway,
  type AgentMutationGateway,
} from '../../agents/AgentMutationGateway';
import type { ConnectionSummary } from '../../../state/gui-view-model';
import type { ChatSeed } from '../../../state/types';
import { UNWIRED_SOURCE, type OrgDomainSource, type OrgStore } from '../domain/context';
import { orgDisplayName } from '../domain/selectors';
import type { OrgDetail, OrgDomainSnapshot, OrgSummary } from '../domain/types';
import { createFixtureGateway } from './fixture-gateway';
import {
  ORGS,
  ORG_PARTNER,
  ORG_PERSONAL,
  ORG_TEAM,
  TEAM_CONFIG_V3,
  inboundCompletedTicket,
  inboundRunningTicket,
  inboundTicket,
  newOrgDetail,
  outboundTicket,
  ownerSnapshot,
  personalDetail,
} from './fixture-data';

export const ORG_DEV_SCENARIOS = [
  { value: 'off', label: '未挂数据源（基线）' },
  { value: 'unwired', label: '数据源未接通' },
  { value: 'owner', label: '工作组织所有者（完整数据）' },
  { value: 'member', label: '工作组织成员视角' },
  { value: 'personal-only', label: '只有个人组织' },
  { value: 'invites', label: '收到多条邀请（登录后确认）' },
  { value: 'partial', label: '创建未完成，需要修复' },
  { value: 'template', label: '新组织：从模板导入' },
  { value: 'migration', label: '迁移源端与迁入请求' },
  { value: 'history-anomaly', label: '空间历史层异常' },
] as const;

export type OrgDevScenario = (typeof ORG_DEV_SCENARIOS)[number]['value'];

export function isOrgDevScenario(value: string): value is OrgDevScenario {
  return ORG_DEV_SCENARIOS.some((option) => option.value === value);
}

export interface OrgDevPreview {
  readonly source: OrgDomainSource;
  readonly agentSeed: AgentSeed;
  readonly agentGateway?: AgentMutationGateway;
  readonly chatSeed: ChatSeed;
}

const CONNECTIONS: readonly ConnectionSummary[] = [
  { id: 'model-connected', kind: 'model', label: '主模型服务', detail: '已连接', status: 'connected', usedBy: 4 },
  { id: 'channel-connected', kind: 'adapter', label: '飞书连接', detail: '已连接', status: 'connected', usedBy: 1 },
];

const MY_AGENTS: readonly AgentSeedRecord[] = [
  {
    id: 'agent-squire',
    displayName: 'Squire',
    role: '个人助理 · 统一入口',
    avatarTone: 'green',
    modelServiceId: 'model-connected',
    channelServiceId: 'channel-connected',
    organizationId: ORG_PERSONAL,
    modelLabel: '主模型服务',
    capabilities: ['日程整理', '消息归档'],
  },
  {
    id: 'agent-researcher',
    displayName: '研究者',
    role: '研究分析',
    avatarTone: 'lilac',
    modelServiceId: 'model-connected',
    organizationId: ORG_PERSONAL,
    modelLabel: '主模型服务',
    capabilities: ['资料检索', '对比分析'],
  },
  {
    id: 'agent-writer',
    displayName: '写作者',
    role: '内容写作',
    avatarTone: 'peach',
    modelServiceId: 'model-connected',
    organizationId: ORG_PERSONAL,
    modelLabel: '主模型服务',
    capabilities: ['长文撰写'],
  },
  {
    id: 'agent-ops',
    displayName: '运营执行',
    role: '运营协调',
    avatarTone: 'grey',
    organizationId: ORG_PERSONAL,
    modelLabel: '未绑定模型',
    capabilities: ['发布协调'],
    disabled: true,
  },
];

const NETWORK_AGENTS: readonly AgentSeedRecord[] = [
  {
    id: 'agent-lin-review',
    displayName: '评审助手',
    role: '代码与设计评审',
    avatarTone: 'lilac',
    modelServiceId: 'model-connected',
    organizationId: ORG_TEAM,
    modelLabel: '主模型服务',
    capabilities: ['评审'],
    owner: '林澄',
    host: 'lin-mbp',
  },
  {
    id: 'agent-chen-build',
    displayName: '构建守护',
    role: '构建与发布',
    avatarTone: 'peach',
    modelServiceId: 'model-connected',
    organizationId: ORG_TEAM,
    modelLabel: '主模型服务',
    capabilities: ['构建'],
    owner: '陈墨',
    host: 'chen-linux',
  },
];

function agentSeed(orgs: readonly OrgSummary[], withNetwork: boolean): AgentSeed {
  return {
    connections: CONNECTIONS,
    organizations: orgs.map((org) => ({ id: org.id, label: orgDisplayName(org) })),
    agents: withNetwork ? [...MY_AGENTS, ...NETWORK_AGENTS] : MY_AGENTS,
  };
}

const CHAT_SEED: ChatSeed = {
  availableTargetId: 'agent-squire',
  sendMode: 'reply',
  replyText: '收到，我来处理。',
  threads: {
    'agent-squire': [
      {
        id: 'org-chat-squire-0',
        targetId: 'agent-squire',
        author: 'user',
        text: '帮我约林澄明天评审组织页。',
        delivery: 'sent',
      },
      {
        id: 'org-chat-squire-0r',
        targetId: 'agent-squire',
        author: 'assistant',
        text: '好的，已把评审请求发给林澄的评审助手。',
        delivery: 'sent',
      },
      {
        id: 'org-chat-squire-1',
        targetId: 'agent-squire',
        author: 'user',
        text: '顺便让陈墨的构建守护跑一遍构建。',
        delivery: 'sent',
      },
      // 开发数据只能让一个 Agent 可发送（availableTargetId），被拒的消息也放在 Squire 会话里。
      {
        id: 'org-chat-squire-2',
        targetId: 'agent-squire',
        author: 'user',
        text: '把这份调研结论发给许沐。',
        delivery: 'failed',
      },
    ],
  },
};

/**
 * Agent 页的内存写入（与 J2 开发数据相同的本地网关）。新建成功时把表单选的组织
 * 写进组织数据，Agent 卡片随即显示该组织标签。
 */
function linkedAgentGateway(seed: AgentSeed, store: () => OrgStore | null): AgentMutationGateway {
  const base = createLocalAgentGateway({
    agents: seed.agents.map((record) => buildManagedAgent(record, seed.connections, seed.organizations)),
    connections: seed.connections,
    organizations: seed.organizations,
  });
  return {
    update: (agentId, draft) => base.update(agentId, draft),
    delete: (agentId) => base.delete(agentId),
    async create(draft) {
      const result = await base.create(draft);
      const target = store();
      const orgId = draft.organizationId;
      if (result.kind === 'created' && target && orgId) {
        target.update((current) => ({
          ...current,
          agentOrgs: { ...(current.agentOrgs ?? {}), [result.agent.id]: [orgId] },
        }));
      }
      return result;
    },
  };
}

function personalOnly(): OrgDomainSnapshot {
  const base = ownerSnapshot();
  return {
    ...base,
    orgs: [ORGS[ORG_PERSONAL]],
    currentOrgId: ORG_PERSONAL,
    details: { [ORG_PERSONAL]: personalDetail() },
    receivedInvites: [],
    inboundMigrations: [],
    agentOrgs: {
      'agent-squire': [ORG_PERSONAL],
      'agent-researcher': [ORG_PERSONAL],
      'agent-writer': [ORG_PERSONAL],
      'agent-ops': [ORG_PERSONAL],
    },
    agentLinks: { 'agent-squire': [], 'agent-researcher': [], 'agent-writer': [], 'agent-ops': [] },
    refusedMessages: { 'agent-squire': [], 'agent-researcher': [], 'agent-writer': [], 'agent-ops': [] },
    chatRefusals: {},
  };
}

function withDetail(
  snapshot: OrgDomainSnapshot,
  orgId: string,
  next: (detail: OrgDetail) => OrgDetail,
): OrgDomainSnapshot {
  return { ...snapshot, details: { ...snapshot.details, [orgId]: next(snapshot.details[orgId]) } };
}

function withExtraOrg(
  snapshot: OrgDomainSnapshot,
  org: OrgSummary,
  detail: OrgDetail,
): OrgDomainSnapshot {
  return {
    ...snapshot,
    orgs: [...(snapshot.orgs ?? []), org],
    currentOrgId: org.id,
    details: { ...snapshot.details, [org.id]: detail },
  };
}

function build(scenario: Exclude<OrgDevScenario, 'off' | 'unwired'>): OrgDomainSnapshot {
  const owner = { ...ownerSnapshot(), receivedInvites: [] };
  switch (scenario) {
    case 'owner':
      return owner;
    case 'member':
      return withDetail({ ...owner, currentOrgId: ORG_PARTNER }, ORG_PARTNER, (detail) => ({
        ...detail,
        config: {
          adopted: { version: 1, publishedAt: '10-25 10:00', publisher: '林澄', config: TEAM_CONFIG_V3 },
          candidate: {
            version: 2,
            publishedAt: '10-29 14:30',
            publisher: '林澄',
            config: { ...TEAM_CONFIG_V3, norms: { ...TEAM_CONFIG_V3.norms, escalation: '阻塞超过 1 小时上报。' } },
            diff: ['修改规范'],
          },
          spaceId: 'space-lab',
        },
      }));
    case 'personal-only':
      return personalOnly();
    case 'invites':
      return {
        ...personalOnly(),
        receivedInvites: [
          { id: 'inv-design', orgName: 'design-guild', inviterLabel: '周屿', acceptedAt: '10-29 09:12' },
          { id: 'inv-ops', orgName: 'ops-oncall', inviterLabel: '许沐', acceptedAt: '10-29 11:40' },
          { id: 'inv-design-2', orgName: 'design-guild', inviterLabel: '林澄', acceptedAt: '10-29 13:05' },
        ],
      };
    case 'partial':
      return withExtraOrg(
        owner,
        { ...ORGS[ORG_TEAM], id: 'org-research-hub', name: 'research-hub', lifecycle: 'partial' },
        { ...newOrgDetail(), spaces: [{ id: 'space-docs', name: '团队文档', health: 'healthy', writable: true }] },
      );
    case 'template':
      return withExtraOrg(owner, { ...ORGS[ORG_TEAM], id: 'org-growth-team', name: 'growth-team' }, newOrgDetail());
    case 'migration': {
      // 第二台电脑已同步完成（各组织里的同一台机器状态一致），可以作为迁移目标。
      const details = Object.fromEntries(
        Object.entries(owner.details).map(([orgId, detail]) => [
          orgId,
          {
            ...detail,
            machines:
              detail.machines?.map((item) =>
                item.hostname === 'yao-mini' ? { ...item, status: 'online' as const } : item,
              ) ?? null,
          },
        ]),
      );
      return {
        ...owner,
        details,
        inboundMigrations: [inboundTicket(), inboundRunningTicket(), inboundCompletedTicket()],
        outboundMigrations: { 'agent-researcher': outboundTicket('awaiting-target') },
      };
    }
    case 'history-anomaly':
      return withDetail(owner, ORG_TEAM, (detail) => ({
        ...detail,
        spaces:
          detail.spaces?.map((space) =>
            space.id === 'space-archive' ? space : { ...space, health: 'history-anomaly' },
          ) ?? null,
      }));
  }
}

/** 选中情形的数据源与配套开发数据；“未挂数据源”返回 null，App 保持基线。 */
export function buildOrgDevPreview(
  scenario: OrgDevScenario,
  options: { readonly latencyMs?: number } = {},
): OrgDevPreview | null {
  if (scenario === 'off') {
    return null;
  }
  if (scenario === 'unwired') {
    return { source: UNWIRED_SOURCE, agentSeed: agentSeed([], false), chatSeed: {} };
  }
  const initial = build(scenario);
  const orgs = initial.orgs ?? [];
  const withNetwork = orgs.some((org) => org.kind === 'work');
  const seed = agentSeed(orgs, withNetwork);
  let store: OrgStore | null = null;
  return {
    source: {
      initial,
      createGateway: (next) => {
        store = next;
        return createFixtureGateway(next, options);
      },
    },
    agentSeed: seed,
    agentGateway: linkedAgentGateway(seed, () => store),
    chatSeed: withNetwork ? CHAT_SEED : { ...CHAT_SEED, threads: { 'agent-squire': CHAT_SEED.threads?.['agent-squire']?.slice(0, 2) ?? [] } },
  };
}
