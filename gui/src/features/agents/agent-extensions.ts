/**
 * dev-02 private view model for Agent dimensions the shared selectors do
 * not carry yet: runtime state, provider-key grants, capability grants and
 * the preset Squire marker.
 *
 * Every dimension defaults to "unknown" ("状态未确认"). Data only arrives
 * through a seed injected by a composition entry (development preview or a
 * test); the product build has no source for it and must never infer one.
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
  | 'ok'
  | 'missing'
  | 'revoked'
  | 'recipient-pending'
  | 'unseal-failed'
  | 'unknown';

/** One provider grant held by an agent (owner, key tail, revision). */
export interface AgentProviderGrantView {
  readonly provider: string;
  readonly state: AgentGrantState;
  readonly ownerLabel?: string;
  readonly keyTail?: string;
  readonly revision?: number;
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
  online: '在线',
  offline: '离线',
  unknown: '状态未确认',
} as const;

export const AGENT_GRANT_STATE_LABELS = {
  ok: '已授权',
  missing: '未授权',
  revoked: '已撤销',
  'recipient-pending': '等待原主人设备',
  'unseal-failed': '解封失败',
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
  if (state === 'ok') {
    return 'positive';
  }
  if (state === 'missing' || state === 'unseal-failed') {
    return 'warning';
  }
  if (state === 'revoked') {
    return 'muted';
  }
  return 'neutral';
}

/** Single-line description of one provider grant for the credential panel. */
export function providerGrantSummary(grant: AgentProviderGrantView): string {
  const parts: string[] = [grant.provider];
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
