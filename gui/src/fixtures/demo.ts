/**
 * Fixture-only demo data. Imported by the app shell and tests; it must never be
 * presented as real chat, connection or account state.
 */

import type {
  ConnectionSummary,
  ConversationMessage,
  SessionTarget,
} from '../state/gui-view-model';

export interface FixtureAgent extends SessionTarget {
  readonly modelLabel: string;
}

export const FIXTURE_AGENTS: readonly FixtureAgent[] = [
  {
    id: 'agent-squire',
    kind: 'agent',
    displayName: 'Squire',
    role: '个人助理 · 统一入口',
    availability: 'needs-connection',
    avatarTone: 'green',
    modelServiceId: 'model-primary',
    modelLabel: '主模型服务',
    capabilities: ['日程整理', '消息归档', '跨端提醒'],
  },
  {
    id: 'agent-researcher',
    kind: 'agent',
    displayName: '研究者',
    role: '研究分析',
    availability: 'needs-connection',
    avatarTone: 'lilac',
    modelServiceId: 'model-primary',
    modelLabel: '主模型服务',
    capabilities: ['资料检索', '对比分析'],
  },
  {
    id: 'agent-writer',
    kind: 'agent',
    displayName: '写作者',
    role: '内容写作',
    availability: 'needs-connection',
    avatarTone: 'peach',
    modelServiceId: 'model-primary',
    modelLabel: '主模型服务',
    capabilities: ['长文撰写', '风格改写'],
  },
  {
    id: 'agent-ops',
    kind: 'agent',
    displayName: '运营执行',
    role: '运营协调',
    availability: 'disabled',
    avatarTone: 'grey',
    modelLabel: '未绑定模型',
    capabilities: ['发布协调', '任务跟踪'],
  },
];

export const FIXTURE_CONNECTIONS: readonly ConnectionSummary[] = [
  {
    id: 'model-primary',
    kind: 'model',
    label: '主模型服务',
    detail: '连接状态未确认',
    status: 'unconfigured',
    usedBy: 0,
  },
  {
    id: 'model-standby',
    kind: 'model',
    label: '备用模型服务',
    detail: '尚未配置',
    status: 'unconfigured',
    usedBy: 0,
  },
  {
    id: 'feishu-main',
    kind: 'adapter',
    label: '飞书连接',
    detail: '等待授权',
    status: 'unconfigured',
    usedBy: 0,
  },
];

export const FIXTURE_THREADS: Readonly<
  Record<string, readonly ConversationMessage[]>
> = {
  'agent-squire': [],
  'agent-researcher': [],
  'agent-writer': [],
};
