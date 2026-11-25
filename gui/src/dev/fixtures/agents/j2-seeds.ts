/**
 * agents 业务线 fixture：开发场景 M-04 / M-05 / M-06 的种子数据。
 *
 * 自 dev-02 的 `gui/src/dev/j2-scenarios.ts` 迁入（implG1），C2 起随开发
 * fixtures 归位 `gui/src/dev/fixtures/agents/`：场景选择器与网关组装仍在
 * `dev/`，种子数据供开发预览、单元测试与最终集成分支比对消费。
 */

import type { ConnectionSummary } from '../../../state/gui-view-model';
import type { AgentSeed } from '../../../state/agent-selectors';

const M04_CONNECTIONS: readonly ConnectionSummary[] = [
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

const M04_ORGANIZATIONS = [
  { id: 'org-design', label: '设计组' },
  { id: 'org-ops', label: '运营组' },
];

/** M-04：3 张卡片，模型已连接，渠道分别为已连接、未连接渠道、状态未确认。 */
export const M04_SEED: AgentSeed = {
  connections: M04_CONNECTIONS,
  organizations: M04_ORGANIZATIONS,
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

/** M-05：空列表与创建入口。 */
export const M05_SEED: AgentSeed = {
  connections: M04_CONNECTIONS,
  organizations: M04_ORGANIZATIONS,
  agents: [],
};

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

/** M-06：1 个 Agent，模型与渠道均未配置，创建提交禁用。 */
export const M06_SEED: AgentSeed = {
  connections: M06_CONNECTIONS,
  organizations: M04_ORGANIZATIONS,
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
