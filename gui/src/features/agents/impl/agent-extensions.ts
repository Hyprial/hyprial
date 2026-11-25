/**
 * dev-02 private view model for Agent dimensions the shared selectors do
 * not carry yet: runtime state, provider-key grants, the preset Squire marker
 * and fixture-only capability rows.
 *
 * The directory path projects runtime/harness/model from `agent.list`; fixture
 * seeds remain for development journeys. Capability grants have no live method
 * and are not rendered. Every dimension defaults to "unknown" ("状态未确认")
 * and must never be inferred.
 *
 * Vocabulary follows the 0.5.5.3 provider-key ownership design
 * (docs/design/provider-key-ownership-2026-10-29.md §7): keys belong to a
 * user, agents hold grants, and the GUI never displays key material.
 */

/** Fixture id of the predefined Squire agent. */
export const SQUIRE_AGENT_ID = 'agent-squire';

/** Mirrors `hyprial agent list` online/offline; unknown until wired. */
export type AgentRuntimeState = 'online' | 'offline' | 'unknown';

/** Grant states from the provider-key ownership design §7. */
export type AgentGrantState =
  | 'granted'
  | 'ok'
  | 'missing'
  | 'revoked'
  | 'awaiting-key'
  | 'recipient-pending'
  | 'unseal-failed'
  | 'signature-invalid'
  | 'key-not-found'
  | 'unavailable'
  | 'unknown';

/**
 * 模型凭据行的两种来源（设计稿）：用户秘钥授权给 Agent / Agent 自有秘钥。
 * 来源由数据行显式携带；wire 路径只见 user-provider 授权行，agent 自有行
 * 若无 provider 证据只进「自有秘钥」名称区，不推测成模型凭据行。
 */
export type AgentCredentialSource = 'user-granted' | 'agent-own';

/** 用户逐 provider 登录状态；无数据源时缺席（unknown），不从未授权推导。 */
export type ProviderLoginState = 'logged-in' | 'not-logged-in' | 'unknown';

/** One provider grant held by an agent (owner, key tail, revision). */
export interface AgentProviderGrantView {
  readonly provider: string;
  readonly state: AgentGrantState;
  /** 设计稿两种行类型：我的秘钥授权 / Agent 自己的秘钥。 */
  readonly source: AgentCredentialSource;
  readonly ownerLabel?: string;
  readonly keyTail?: string;
  readonly revision?: number;
  /** 授权时间标签；agent.secret.list 不提供时间，仅种子路径可携带。 */
  readonly grantedAtLabel?: string;
}

/** One capability grant row (from `hyprial agent grants`). */
export interface AgentCapabilityGrantView {
  readonly id: string;
  readonly label: string;
  readonly state: 'granted' | 'revoked' | 'unknown';
}

/** dev-02 only dimensions for one agent. */
export interface AgentExtensionView {
  readonly runtime: AgentRuntimeState;
  /** Honest run-state detail from the data source (e.g. 空闲 / 进行中（推断）). */
  readonly runtimeDetail?: string;
  readonly providerGrants: readonly AgentProviderGrantView[];
  readonly capabilityGrants: readonly AgentCapabilityGrantView[];
  readonly preset: boolean;
  readonly preferredHarness?: string;
  /** Runtime fact: the harness the agent actually runs on (not the preference). */
  readonly harness?: string;
  /** Runtime fact: the model currently in use. */
  readonly model?: string;
}

/** Partial overrides by agent id, injected by a composition entry. */
export type AgentExtensionSeed = Readonly<Record<string, Partial<AgentExtensionView>>>;

export const AGENT_RUNTIME_LABELS = {
  online: '运行中',
  offline: '未运行',
  unknown: '状态未确认',
} as const;

export const AGENT_GRANT_STATE_LABELS = {
  granted: '已授权',
  ok: '已授权',
  missing: '未授权',
  revoked: '已撤销',
  'awaiting-key': '等待 Key',
  'recipient-pending': '等待原主人设备',
  'unseal-failed': '解封失败',
  'signature-invalid': '签名无效',
  'key-not-found': 'Key 不存在',
  unavailable: '状态不可用',
  unknown: '状态未确认',
} as const;

export const AGENT_GRANT_EMPTY_NOTE = '暂无授权记录，状态未确认。';

export const EMPTY_AGENT_EXTENSION: AgentExtensionView = {
  runtime: 'unknown',
  providerGrants: [],
  capabilityGrants: [],
  preset: false,
};

export function getAgentExtension(
  seed: AgentExtensionSeed | undefined,
  agentId: string,
): AgentExtensionView {
  const override = seed?.[agentId];
  if (!override) {
    return {
      ...EMPTY_AGENT_EXTENSION,
      preset: agentId === SQUIRE_AGENT_ID,
    };
  }
  return {
    ...EMPTY_AGENT_EXTENSION,
    ...override,
    preset: override.preset ?? agentId === SQUIRE_AGENT_ID,
  };
}

export function runtimeTone(
  runtime: AgentRuntimeState,
): 'positive' | 'muted' | 'neutral' {
  if (runtime === 'online') {
    return 'positive';
  }
  if (runtime === 'offline') {
    return 'muted';
  }
  return 'neutral';
}

export function grantTone(
  state: AgentGrantState,
): 'positive' | 'warning' | 'muted' | 'neutral' {
  if (state === 'ok' || state === 'granted') {
    return 'positive';
  }
  if (
    state === 'missing'
    || state === 'unseal-failed'
    || state === 'awaiting-key'
    || state === 'signature-invalid'
    || state === 'key-not-found'
  ) {
    return 'warning';
  }
  if (state === 'revoked') {
    return 'muted';
  }
  return 'neutral';
}

/** 模型凭据行来源列文案，按行内 source 显式选择，不再固定一种。 */
export const AGENT_CREDENTIAL_SOURCE_LABELS = {
  'user-granted': '我的秘钥 → 授权给此 Agent',
  'agent-own': 'Agent 自己的秘钥',
} as const;

/**
 * 需要提示「去连接页登录」的 provider：登录证据显式为 not-logged-in，
 * 且该 provider 没有可用授权记录。未授权/未知授权状态不是未登录证据。
 */
export function providersMissingLogin(
  grants: readonly AgentProviderGrantView[],
  logins: Readonly<Record<string, ProviderLoginState>> | undefined,
): readonly string[] {
  if (!logins) {
    return [];
  }
  const granted = new Set(
    grants
      .filter((grant) => grant.state === 'ok' || grant.state === 'recipient-pending')
      .map((grant) => grant.provider),
  );
  return Object.entries(logins)
    .filter(
      ([provider, state]) =>
        state === 'not-logged-in' && !granted.has(provider),
    )
    .map(([provider]) => provider);
}

/** 授权记录的补充信息（主人 / keyId / revision），不含 provider 名。 */
export function providerGrantDetail(grant: AgentProviderGrantView): string {
  const parts: string[] = [];
  if (grant.ownerLabel) {
    parts.push(`主人：${grant.ownerLabel}`);
  }
  if (grant.keyTail) {
    parts.push(`keyId …${grant.keyTail}`);
  }
  if (typeof grant.revision === 'number') {
    parts.push(`r${grant.revision}`);
  }
  return parts.join(' · ');
}

/** CLI line that grants this agent a provider key (owner-side action). */
export function providerAllowHint(agentId: string): string {
  return `hyprial agent provider allow --actor ${agentId} --provider <provider>`;
}

/** The capability-grant list command this section mirrors (agent grant, not agent secret). */
export function capabilityGrantListHint(agentId: string): string {
  return `hyprial agent grant ${agentId} list`;
}
