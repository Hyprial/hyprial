/**
 * agents 业务线 fixture：四页签详情（implG4）各态。
 *
 * 覆盖设计稿（mock/agent-detail-mock.html）中出现的状态：模型凭据每个状态芯片、
 * 空状态、暂不可用槽位（最近活动 / routine / home-census / cell / PAC 工作流 /
 * adapter）。只含展示用
 * 示例数据，不含凭据明文；授权时间等 agent.secret.list 未提供的字段一律
 * 由组件显示为状态未确认。
 */

import type { AgentExtensionView, AgentCredentialSource } from '../../../src/features/agents/impl/agent-extensions';
import type {
  AgentsListRowFixture,
  AgentsSecretGrantFixture,
} from './agent-list';

/** 带 home / 活动事实的目录行（agent.list 本线消费字段 + cwd/activityHints）。 */
export interface AgentsDetailRowFixture extends AgentsListRowFixture {
  readonly cwd?: string | null;
  readonly lastActiveAtMs?: number | null;
  readonly activityHints?: {
    readonly agentHomeMtimeMs: number | null;
    readonly createdAtMs: number;
    readonly hasLastSessionId: boolean;
  };
}

/** 概览页签样例：研究者（在线，cwd 与活动事实齐备，capabilities 为真键投影）。 */
export const AGENTS_DETAIL_ROWS_HOME: readonly AgentsDetailRowFixture[] = [
  {
    actor: 'researcher',
    uri: 'agent:owner:example-hq:a.researcher',
    owner: '',
    machine: 'example-hq',
    provider: 'claude',
    model: 'claude-sonnet-4.6',
    harness: 'claude',
    preferredHarness: 'claude',
    capabilities: { 资料检索: true, 对比分析: true, 长文撰写: false },
    cwd: '~/.hyprial/agents/researcher',
    lastActiveAtMs: 1793604120000,
    activityHints: {
      agentHomeMtimeMs: 1793604060000,
      createdAtMs: 1760000000000,
      hasLastSessionId: true,
    },
    runState: {
      state: 'idle',
      certainty: 'observed',
      observationHealth: 'current',
    },
  },
];

/** 概览页签：home / 活动事实缺失（→ 暂不可用 / 状态未确认，不编造）。 */
export const AGENTS_DETAIL_ROWS_NO_FACTS: readonly AgentsDetailRowFixture[] = [
  {
    actor: 'minimal',
    uri: 'agent:owner:node:a.minimal',
    owner: '',
    machine: '',
    provider: null,
    model: null,
    harness: null,
    preferredHarness: null,
    runState: {
      state: 'unknown',
      certainty: 'unknown',
      observationHealth: 'unavailable',
    },
  },
];

/** 凭据与授权页签：user-provider 授权 + agent 自有秘钥（互不混区）。 */
export const AGENTS_TABS_GRANTS_MIXED: readonly AgentsSecretGrantFixture[] = [
  {
    grantId: 'grant-up-1',
    actor: 'researcher',
    source: 'user-provider',
    fieldName: 'anthropic',
    ownerActor: 'allen',
    ownerEntityToken: 'tok',
    revision: 3,
    environmentNames: ['default'],
  },
  {
    grantId: 'grant-priv-1',
    actor: 'researcher',
    source: 'agent-private',
    fieldName: 'GITHUB_TOKEN',
    ownerActor: null,
    ownerEntityToken: null,
    revision: 2,
    environmentNames: [],
  },
  {
    grantId: 'grant-shared-1',
    actor: 'researcher',
    source: 'agent-shared',
    fieldName: 'NOTION_API_KEY',
    ownerActor: null,
    ownerEntityToken: null,
    revision: 1,
    environmentNames: [],
  },
];

/** 扩展覆盖：模型凭据每个状态芯片各一行（含授权时间标签，仅种子路径可得）。 */
export function extensionWithGrantStates(
  states: readonly {
    provider: string;
    state: 'ok' | 'missing' | 'revoked' | 'recipient-pending' | 'unseal-failed' | 'unknown';
    /** 设计稿两种行型；缺省为用户秘钥授权（G4-4）。 */
    source?: AgentCredentialSource;
    grantedAtLabel?: string;
  }[],
  overrides: Partial<AgentExtensionView> = {},
): AgentExtensionView {
  return {
    runtime: 'online',
    providerGrants: states.map((grant) => ({
      provider: grant.provider,
      state: grant.state,
      source: grant.source ?? ('user-granted' as const),
      grantedAtLabel: grant.grantedAtLabel,
    })),
    capabilityGrants: [],
    preset: false,
    ...overrides,
  };
}

/**
 * implS2：运行/关联页签新接线方法的行 fixture（routine.list / workflow.list /
 * agent.home-census / routine.status）。只含展示用示例数据，字段与契约声明
 * 一致；缺失字段由组件显示 — / 状态未确认。
 */

export interface AgentsRoutineRowFixture {
  readonly name: string;
  readonly owner: string | null;
  readonly actor: string;
  readonly actorOwnership: string;
  readonly enabled: boolean;
  readonly mode: string | null;
  readonly role: string | null;
  readonly nextDueMs: number | null;
  readonly lastOutcome: string | null;
  readonly lastDispatchAtMs: number | null;
  readonly sourceErrorStreak: number;
  readonly missedPeriods: number | null;
  readonly quarantined: boolean;
  readonly quarantineReason: string | null;
  readonly registrationId: string | null;
  readonly readable: boolean;
  readonly inFlight: readonly unknown[];
  readonly launch: Record<string, unknown> | null;
}

export const AGENTS_ROUTINE_ROWS: readonly AgentsRoutineRowFixture[] = [
  {
    name: 'heartbeat',
    owner: 'user:acme',
    actor: 'agent:acme:node-1:researcher',
    actorOwnership: 'external',
    enabled: true,
    mode: 'scheduled',
    role: 'dispatch',
    nextDueMs: 1793604060000,
    lastOutcome: 'done',
    lastDispatchAtMs: 1793604000000,
    sourceErrorStreak: 0,
    missedPeriods: null,
    quarantined: false,
    quarantineReason: null,
    registrationId: null,
    readable: true,
    inFlight: [],
    launch: null,
  },
  {
    name: 'triage',
    owner: 'user:acme',
    actor: 'agent:acme:node-1:researcher',
    actorOwnership: 'external',
    enabled: false,
    mode: 'scheduled',
    role: 'dispatch',
    nextDueMs: null,
    lastOutcome: 'error',
    lastDispatchAtMs: null,
    sourceErrorStreak: 2,
    missedPeriods: 1,
    quarantined: true,
    quarantineReason: 'repeated failures',
    registrationId: 'reg-1',
    readable: true,
    inFlight: [{ taskUuid: 't1', runId: 'wf-9', target: 'agent:acme:node-1:researcher' }],
    launch: { tier: 'fast', harness: 'codex', provider: null, model: 'gpt-5.6-luna', args: [] },
  },
];

export interface AgentsRoutineStatusFixture extends AgentsRoutineRowFixture {
  readonly outcomes: readonly string[];
  readonly scheduleEvents: readonly {
    readonly routine: string;
    readonly slot_ms: number | null;
    readonly disposition: string | null;
    readonly count: number | null;
    readonly reason: string | null;
    readonly recorded_at_ms: number | null;
  }[];
}

export const AGENTS_ROUTINE_STATUS: AgentsRoutineStatusFixture = {
  ...AGENTS_ROUTINE_ROWS[0],
  outcomes: ['done', 'done', 'skipped'],
  scheduleEvents: [
    {
      routine: 'heartbeat',
      slot_ms: 1793604060000,
      disposition: 'dispatched',
      count: 1,
      reason: 'timer',
      recorded_at_ms: 1793604060005,
    },
  ],
};

export interface AgentsWorkflowRunFixture {
  readonly runId: string;
  readonly graphId: string | null;
  readonly name: string | null;
  readonly state: string;
  readonly currentNode: { readonly nodeId: string; readonly state: string } | null;
  readonly createdAtMs: number | null;
  readonly closedAtMs: number | null;
  readonly onFailure: string | null;
  readonly sender: string | null;
}

export const AGENTS_WORKFLOW_ROWS: readonly AgentsWorkflowRunFixture[] = [
  {
    runId: 'wf-0001',
    graphId: 'wf-0001',
    name: 'brief-relations',
    state: 'running',
    currentNode: { nodeId: 'work', state: 'requested' },
    createdAtMs: 1793600000000,
    closedAtMs: null,
    onFailure: 'terminate',
    sender: 'user:acme',
  },
  {
    runId: 'wf-0002',
    graphId: 'wf-0002',
    name: 'nightly-inventory',
    state: 'completed',
    currentNode: null,
    createdAtMs: 1793510000000,
    closedAtMs: 1793514000000,
    onFailure: 'terminate',
    sender: 'user:acme',
  },
];

export interface AgentsCensusRowFixture {
  readonly actor: string;
  readonly uri: string;
  readonly homeProvisioned: boolean;
  readonly explicitConfig: boolean;
  readonly configInsideHome: boolean;
  readonly cwdInsideHome: boolean;
  readonly residentOrDsh: boolean;
  readonly transferReceived: boolean;
  readonly mode: 'p2' | 'legacy';
  readonly legacyReason: string | null;
  readonly harness: string | null;
  readonly lastHarness: string | null;
}

export const AGENTS_CENSUS_ROW: AgentsCensusRowFixture = {
  actor: 'researcher',
  uri: 'agent:acme:node-1:researcher',
  homeProvisioned: true,
  explicitConfig: true,
  configInsideHome: true,
  cwdInsideHome: true,
  residentOrDsh: false,
  transferReceived: false,
  mode: 'p2',
  legacyReason: null,
  harness: 'claude',
  lastHarness: 'claude',
};

export function routineListResponse(
  rows: readonly AgentsRoutineRowFixture[],
): Record<string, unknown> {
  return { ok: true, routines: rows.map((row) => ({ ...row, inFlight: [...row.inFlight] })) };
}

export function routineStatusResponse(
  row: AgentsRoutineStatusFixture,
): Record<string, unknown> {
  return {
    ok: true,
    ...row,
    inFlight: [...row.inFlight],
    outcomes: [...row.outcomes],
    scheduleEvents: row.scheduleEvents.map((event) => ({ ...event })),
  };
}

export function workflowListResponse(
  rows: readonly AgentsWorkflowRunFixture[],
): Record<string, unknown> {
  return { ok: true, runs: rows.map((row) => ({ ...row })) };
}

export function censusResponse(
  row: AgentsCensusRowFixture | null,
): Record<string, unknown> {
  return {
    ok: true,
    readAtMs: 1793604060000,
    registry: { path: '/tmp/hyprial-example/state/agents.sqlite3', version: 1 },
    agents: row === null ? [] : [{ ...row }],
    totals: {
      agents: row === null ? 0 : 1,
      byModeHarness: row === null ? [] : [{ mode: row.mode, harness: row.harness, count: 1 }],
      homeUnprovisioned: 0,
    },
  };
}
