/**
 * agents 业务线 fixture：`agent.list` 读取侧的行数据（implG1）。
 *
 * 形状对齐生成 client 的 `AgentListResult['agents'][number]` 中本线消费的字段；
 * 只声明映射层实际读取的字段，未声明字段视为该来源未提供。空列表、有数据、
 * 跨所有者分组三种列表场景共用本文件；运行状态各态（含推断、观测过期）
 * 也在这里覆盖，供 api 映射测试与目录页测试消费。
 */

export interface AgentsListRowFixture {
  readonly actor: string;
  readonly uri: string;
  readonly displayName?: string | null;
  readonly owner: string;
  readonly machine: string;
  readonly provider: string | null;
  readonly model: string | null;
  readonly cwd?: string | null;
  readonly harness: string | null;
  readonly preferredHarness: string | null;
  /** agent.list capabilities 原始对象；映射层投影值为真的键名（G4-3）。 */
  readonly capabilities?: Record<string, unknown>;
  readonly runState?: {
    readonly state:
      | 'blocked'
      | 'idle'
      | 'in-turn'
      | 'not-running'
      | 'starting'
      | 'stopping'
      | 'suspected-stuck'
      | 'unknown';
    readonly certainty: 'inferred' | 'observed' | 'unknown';
    readonly observationHealth:
      | 'current'
      | 'partial'
      | 'resync-required'
      | 'stale'
      | 'unavailable';
  };
}

/** M-05：空列表。 */
export const AGENTS_LIST_ROWS_EMPTY: readonly AgentsListRowFixture[] = [];

/**
 * M-04（读取侧形态）：有数据且跨所有者分组。
 * 「我」的两条 + alice 的两条；运行状态覆盖 observed 空闲、observed 未运行、
 * inferred 进行中（partial）、观测过期（stale → 状态未确认）。
 */
export const AGENTS_LIST_ROWS_DATA: readonly AgentsListRowFixture[] = [
  {
    actor: 'squire',
    uri: 'agent:owner:node:a.squire',
    owner: '',
    machine: '',
    provider: 'claude',
    model: 'claude-sonnet',
    harness: 'pi',
    preferredHarness: 'pi',
    runState: {
      state: 'idle',
      certainty: 'observed',
      observationHealth: 'current',
    },
  },
  {
    actor: 'researcher',
    uri: 'agent:owner:lab-2:a.researcher',
    owner: '',
    machine: 'lab-2',
    provider: null,
    model: null,
    harness: null,
    preferredHarness: null,
    runState: {
      state: 'not-running',
      certainty: 'observed',
      observationHealth: 'current',
    },
  },
  {
    actor: 'writer',
    uri: 'agent:alice:home-1:a.writer',
    owner: 'alice',
    machine: 'home-1',
    provider: 'codex',
    model: null,
    harness: 'codex',
    preferredHarness: null,
    runState: {
      state: 'in-turn',
      certainty: 'inferred',
      observationHealth: 'partial',
    },
  },
  {
    actor: 'archivist',
    uri: 'agent:alice:home-1:a.archivist',
    owner: 'alice',
    machine: 'home-1',
    provider: null,
    model: null,
    harness: null,
    preferredHarness: null,
    runState: {
      state: 'blocked',
      certainty: 'observed',
      observationHealth: 'stale',
    },
  },
];

/** 无 runState 字段的行：运行状态只能显示「状态未确认」。 */
export const AGENTS_LIST_ROW_NO_RUNSTATE: readonly AgentsListRowFixture[] = [
  {
    actor: 'minimal',
    uri: 'agent:owner:node:a.minimal',
    owner: '',
    machine: '',
    provider: null,
    model: null,
    harness: null,
    preferredHarness: null,
  },
];

/** `agent.secret.list` 的授权行 fixture（详情读取侧）。 */
export interface AgentsSecretGrantFixture {
  readonly grantId: string;
  readonly actor: string;
  readonly source: 'agent-private' | 'agent-shared' | 'user-provider';
  readonly fieldName: string | null;
  readonly ownerActor: string | null;
  readonly ownerEntityToken: string | null;
  readonly revision: number;
  readonly environmentNames: readonly string[];
}

/** squire 持有一条 user-provider 授权；agent 自有秘钥行不进模型凭据区。 */
export const AGENTS_SECRET_GRANTS_SQUIRE: readonly AgentsSecretGrantFixture[] = [
  {
    grantId: 'grant-1',
    actor: 'squire',
    source: 'user-provider',
    fieldName: 'api_key',
    ownerActor: 'allen',
    ownerEntityToken: 'tok',
    revision: 3,
    environmentNames: ['default'],
  },
  {
    grantId: 'grant-2',
    actor: 'squire',
    source: 'agent-private',
    fieldName: 'token',
    ownerActor: null,
    ownerEntityToken: null,
    revision: 1,
    environmentNames: [],
  },
];

/** 读取成功但没有任何 user-provider 授权：显示「未授权」，不是状态未确认。 */
export const AGENTS_SECRET_GRANTS_EMPTY: readonly AgentsSecretGrantFixture[] = [];
