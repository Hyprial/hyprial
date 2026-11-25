/**
 * agents 业务线 fixture：详情与管理面板各态（implG1）。
 *
 * 详情态（可用 / 需绑定模型凭据 / 已停用 / 状态未确认）以 `ManagedAgentView`
 * 形状给出；管理面板各态以 `AgentExtensionView` 覆盖给出（provider grant
 * ok / revoked / recipient-pending / unseal-failed）。供组件测试与跨线
 * cherry-pick 消费；不含凭据明文。
 */

import type { ManagedAgentView } from '../../../src/state/agent-selectors';
import type { AgentExtensionView } from '../../../src/features/agents/impl/agent-extensions';

function baseView(overrides: Partial<ManagedAgentView>): ManagedAgentView {
  return {
    id: 'agent-fixture',
    displayName: '详情样例',
    role: '样例职责',
    avatarTone: 'green',
    modelServiceId: null,
    channelServiceId: null,
    organizationId: null,
    modelLabel: '未指定模型',
    capabilities: [],
    organizationLabel: '未加入组织',
    ownerLabel: '我',
    host: '本机',
    availability: 'needs-connection',
    model: {
      kind: 'unconfigured',
      label: '需绑定模型凭据',
      sourceLabel: '需绑定模型凭据',
    },
    channel: {
      kind: 'unknown',
      label: '状态未确认',
      sourceLabel: '状态未确认',
    },
    ...overrides,
  };
}

/** 可用：模型凭据已连接。 */
export const AGENTS_DETAIL_AVAILABLE: ManagedAgentView = baseView({
  id: 'agent-detail-available',
  displayName: '可用 Agent',
  availability: 'available',
  model: {
    kind: 'connected',
    label: '已连接',
    sourceLabel: '主模型服务',
  },
});

/** 需绑定模型凭据。 */
export const AGENTS_DETAIL_NEEDS_CONNECTION: ManagedAgentView = baseView({
  id: 'agent-detail-needs-connection',
  displayName: '待绑定 Agent',
  availability: 'needs-connection',
});

/** 已停用（运营执行）。 */
export const AGENTS_DETAIL_DISABLED: ManagedAgentView = baseView({
  id: 'agent-detail-disabled',
  displayName: '已停用 Agent',
  availability: 'disabled',
  model: {
    kind: 'disabled',
    label: '已停用',
    sourceLabel: '主模型服务',
  },
});

/** 状态未确认：缺数据时不判定可用或需连接。 */
export const AGENTS_DETAIL_UNKNOWN: ManagedAgentView = baseView({
  id: 'agent-detail-unknown',
  displayName: '未确认 Agent',
  availability: 'unknown',
  model: {
    kind: 'unknown',
    label: '状态未确认',
    sourceLabel: '状态未确认',
  },
});

/** 管理面板：provider grant 各态（运行状态在线）。 */
export const AGENTS_MANAGE_GRANT_OK: AgentExtensionView = {
  runtime: 'online',
  providerGrants: [
    { provider: 'claude', state: 'ok', source: 'user-granted', ownerLabel: 'allen', keyTail: '9f2e', revision: 3 },
  ],
  capabilityGrants: [],
  preset: false,
};

export const AGENTS_MANAGE_GRANT_REVOKED: AgentExtensionView = {
  runtime: 'offline',
  providerGrants: [
    { provider: 'claude', state: 'revoked', source: 'user-granted', ownerLabel: 'allen', revision: 4 },
  ],
  capabilityGrants: [],
  preset: false,
};

export const AGENTS_MANAGE_GRANT_RECIPIENT_PENDING: AgentExtensionView = {
  runtime: 'unknown',
  providerGrants: [
    { provider: 'codex', state: 'recipient-pending', source: 'user-granted', ownerLabel: 'allen', revision: 2 },
  ],
  capabilityGrants: [],
  preset: false,
};

export const AGENTS_MANAGE_GRANT_UNSEAL_FAILED: AgentExtensionView = {
  runtime: 'unknown',
  providerGrants: [
    { provider: 'pi', state: 'unseal-failed', source: 'user-granted', ownerLabel: 'allen', revision: 7 },
  ],
  capabilityGrants: [],
  preset: false,
};
