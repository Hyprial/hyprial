/**
 * GUI live A (2026-11-04): createAgentsApi consumes an injected AgentsClient.
 * implG1 (2026-11-02) 决策记录：
 * - 本文件是 agents 读取侧唯一数据层：组件 → api → 生成 client，组件不 fetch、
 *   不持有第二份 DTO。方法面为 agent.list / agent.secret.list（H-01 读）与
 *   agent.provider.default（mutates=true，HTTP 上整个方法只经 H-03）。
 * - 运行状态映射：certainty=unknown 或观测过期（stale/unavailable/resync-required）
 *   一律「状态未确认」；inferred 显示但标注（推断）；无 runState 字段不推测。
 * - revG1 G1-1（2026-11-02 round 2）：agent.secret.list 只是授权 inventory，
 *   无秘钥值与接收/解封状态，不得推导凭据可用性。读取侧不存在任何把
 *   授权记录提升为「已连接/可用」的映射（round 1 的 credentialEvidenceOf /
 *   applyCredentialEvidence 已删）；列表与详情的凭据连接一律「状态未确认」，
 *   授权记录本身在管理面板以「已授权」inventory 呈现；开始对话不因授权
 *   记录存在而启用。
 * - revG1 G1-2（2026-11-02 round 2）：modelLabel 同时携带 provider 与实际
 *   model（row.model），搜索语料两者都命中；无任何一方时回退「未指定模型」。
 * - agent.secret.list 的 agent-private/agent-shared 行是 Agent 自有服务秘钥，
 *   不进「模型凭据」区（dev-02 规则）。
 * - provider 名称未出现在 agent.secret.list 结果里，展示用 fieldName 标识；
 *   provider 默认值权威在用户设置（user-settings），本线只显示状态并链接出去。
 * - implG2 (2026-11-02)：写侧加入 agent.create / agent.stop，
 *   全部经生成 client 与契约类型；错误归一为 AgentRequestError（见
 *   agent-writes.ts）。列表行投影抽出 rowOfAgentEntity，创建结果实体与
 *   列表行共用同一投影，不产生第二份 DTO。
 * - G1 (2026-11-22)：移除 GUI 对 link.list 的调用与 listLinks / AgentLinkRow 投影。
 * - implG4 (2026-11-02)：agent.list 的 home/活动事实投影（cwd/activityHints/lastActiveAtMs）；
 *   目录行与详情页共用同一投影，不产生第二份 DTO。
 * - implG5 (2026-11-02, G4-3/G4-4)：agent.list 的 capabilities 投影进目录行
 *   （值为真的键名），详情页能力不再永远为空；grantRowsToProviderGrants
 *   显式标注来源 source='user-granted'（与设计稿的 agent-own 行型区分）。
 * - implS2 (2026-11-02, second submission)：api 包装器对 routine.list/status、
 *   workflow.list 的 ok-less 结果放行（envelope 层已保证 ok，声明结果不再
 *   重复 ok 字段）；携带 ok 的结果仍拒绝 ok:false。五个新方法全部经生成
 *   client（start retry=never；读方法 retry=safe）；读投影只用声明字段。
 * - UUID addressing (2026-11-07)：行的 displayName 取实体的非空 displayName
 *   （agent.rename 写入），缺省使用明确的本机创建名 actor（2026-11-15 用户修正）；
 *   不从 URI 截取名称。id、avatar 与全部操作仍以不可变的
 *   actor 为键，改名不改变操作对象。
 */

import { modelActions, type ModelActions } from '../model-configuration/api';
import { secretProviderActions } from './secret-keys';
import { callAgentMethod, withPresentHandlers, type AgentsClient } from './client';
import type { AgentDestroyPreviewResult, AgentDestroyResult } from '../../../../client/generated/types';
import type { MethodAccess } from '../../../../client';
export { AGENTS_CLIENT_METHODS, type AgentsClient } from './client';
import {
  AgentRequestError,
  toAgentRequestError,
  type AgentCreateInput,
  type AgentCreateOutcome,
  type AgentStartOptions,
  type AgentStartOutcome,
  type AgentStopOutcome,
} from './agent-writes';
import type {
  AgentProviderGrantView,
  AgentExtensionView,
  AgentRuntimeState,
} from '../agent-extensions';
import type { ManagedAgentView } from '../../../../state/agent-selectors';

export {
  AgentRequestError,
  agentFailureMessage,
  startFailureMessage,
  toAgentRequestError,
} from './agent-writes';
export {
  createContractAgentGateway,
  createRuntimeActionHandler,
  type ContractGatewayOptions,
  type RuntimeActionHandler,
  type RuntimeActionKind,
  type RuntimeActionOutcome,
} from './contract-gateway';
export type {
  AgentCreateInput,
  AgentCreateOutcome,
  AgentStartOptions,
  AgentStartOutcome,
  AgentStopOutcome,
} from './agent-writes';

/** Run-state badge facts for one agent, already projected to UI vocabulary. */
export interface AgentRunStateFacts {
  readonly runtime: AgentRuntimeState;
  readonly runtimeDetail?: string;
  readonly summaryState?: 'normal' | 'abnormal';
}

/** One directory row from `agent.list`, in this feature's view vocabulary. */
export interface AgentDirectoryRow {
  readonly actor: string;
  readonly agentId?: string | null;
  readonly uri: string;
  readonly displayName: string;
  readonly owner: string;
  readonly host: string;
  readonly provider: string | null;
  readonly model: string | null;
  readonly harness: string | null;
  readonly preferredHarness: string | null;
  /** Only curated product capabilities belong in visible badges. */
  readonly capabilities: readonly string[];
  readonly modelConfigurationRequired?: boolean;
  readonly runState: AgentRunStateFacts;
  readonly startupFailure?: { readonly operationId: string; readonly message: string };
  /** agent.list 本线消费的 home / 活动事实；未提供时为 null（不推测）。 */
  readonly cwd: string | null;
  readonly homeMtimeMs: number | null;
  readonly createdAtMs: number | null;
  readonly hasLastSessionId: boolean | null;
  readonly lastActiveAtMs: number | null;
}

/** One grant row from `agent.secret.list`. */
export interface AgentSecretGrantRow {
  readonly grantId: string;
  readonly source: 'agent-private' | 'agent-shared' | 'user-provider';
  readonly fieldName: string | null;
  readonly ownerActor: string | null;
  readonly revision: number;
}

/** Provider default status; authority stays with user settings. */
export interface ProviderDefaultStatus {
  readonly ready: boolean;
  readonly provider: string | null;
}

/** routine 的 launch 摘要（声明键集的子集投影，缺失即 null）。 */
export interface AgentRoutineLaunch {
  readonly tier: string | null;
  readonly harness: string | null;
  readonly provider: string | null;
  readonly model: string | null;
}

/** One `routine.list` row (also the base of `routine.status`). */
export interface AgentRoutineRow {
  readonly name: string;
  readonly owner: string | null;
  readonly actor: string;
  readonly actorOwnership: string | null;
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
  readonly inFlightCount: number;
  readonly launch: AgentRoutineLaunch | null;
}

/** One `routine.status` row: the list row plus the live schedule facts. */
export interface AgentRoutineStatus extends AgentRoutineRow {
  readonly outcomes: readonly string[];
  readonly scheduleEvents: readonly {
    readonly routine: string;
    readonly slotMs: number | null;
    readonly disposition: string | null;
    readonly count: number | null;
    readonly reason: string | null;
    readonly recordedAtMs: number | null;
  }[];
}

/** One `workflow.list` run row. */
export interface AgentWorkflowRun {
  readonly runId: string;
  readonly graphId: string | null;
  readonly name: string | null;
  readonly state: string;
  readonly currentNode: {
    readonly nodeId: string;
    readonly state: string;
  } | null;
  readonly createdAtMs: number | null;
  readonly closedAtMs: number | null;
  readonly onFailure: string | null;
  readonly sender: string | null;
}

/** One agent row of `agent.home-census` (the declared home/migration facts). */
export interface AgentHomeCensusRow {
  readonly actor: string;
  readonly uri: string | null;
  readonly homeProvisioned: boolean;
  readonly explicitConfig: boolean;
  readonly configInsideHome: boolean;
  readonly cwdInsideHome: boolean;
  readonly residentOrDsh: boolean;
  readonly transferReceived: boolean;
  readonly mode: 'p2' | 'legacy' | null;
  readonly legacyReason: string | null;
  readonly harness: string | null;
  readonly lastHarness: string | null;
}

/** `agent.home-census` for one selected agent. */
export interface AgentHomeCensus {
  readonly readAtMs: number | null;
  readonly row: AgentHomeCensusRow | null;
  readonly totals: {
    readonly agents: number;
    readonly homeUnprovisioned: number;
  };
}

/** One saved Secret grant observation; it does not prove authentication. */
export interface AgentProviderGrant {
  readonly grantId: string;
  readonly provider: string;
  readonly revision?: number;
  readonly state:
    | 'granted'
    | 'ok'
    | 'missing'
    | 'revoked'
    | 'awaiting-key'
    | 'recipient-pending'
    | 'unseal-failed'
    | 'signature-invalid'
    | 'key-not-found'
    | 'unavailable';
  readonly code: string | null;
  readonly owner: string | null;
  readonly keyId: string | null;
  readonly expiresAtMs: number | null;
  readonly unsealable: boolean;
}

/** Optional incarnation fence, only forwarded by methods that declare it. */
export interface AgentExpected {
  readonly uri: string;
  readonly createdAtMs: number;
}

export interface AgentProviderAllowOptions {
  readonly grantId?: string;
  readonly revision?: number;
  readonly keyId?: string | null;
  readonly replace?: boolean;
  readonly expectedAgent?: AgentExpected | null;
}

export interface AgentProviderWriteOutcome {
  readonly changed: boolean;
  readonly provider: string;
  /** False unless the daemon reports the credential already took effect without restart. */
  readonly restartApplied: boolean;
}

export interface AgentNativeFileWrite {
  readonly path: string;
  readonly bytes: number;
  readonly replaced: boolean;
  readonly unchanged: boolean;
}

export interface AgentNativeLoginOutcome {
  readonly actor: string;
  readonly provider: string;
  readonly files: readonly AgentNativeFileWrite[];
}

export interface AgentsApi extends Partial<ModelActions> {
  previewDestroy?(name: string): Promise<AgentDestroyPreviewResult>;
  destroy?(operationId: string): Promise<AgentDestroyResult>;
  list?(): Promise<readonly AgentDirectoryRow[]>;
  listGrants?(actor: string): Promise<readonly AgentSecretGrantRow[]>;
  defaultProvider?(): Promise<ProviderDefaultStatus>;
  create?(input: AgentCreateInput): Promise<AgentCreateOutcome>;
  stop?(name: string): Promise<AgentStopOutcome>;
  start?(name: string, options?: AgentStartOptions): Promise<AgentStartOutcome>;
  rename?(actor: string, displayName: string): Promise<AgentDirectoryRow>;
  providerStatus?(
    actor: string,
    expectedAgent?: AgentExpected | null,
  ): Promise<readonly AgentProviderGrant[]>;
  allowProvider?(
    actor: string,
    provider: string,
    options?: AgentProviderAllowOptions,
  ): Promise<AgentProviderWriteOutcome>;
  revokeProvider?(
    actor: string,
    provider: string,
    expectedAgent?: AgentExpected | null,
  ): Promise<AgentProviderWriteOutcome>;
  loginNative?(
    actor: string,
    provider: string,
    files: readonly { readonly filename: string; readonly contentBase64: string }[],
    options?: { readonly replace?: boolean },
  ): Promise<AgentNativeLoginOutcome>;
  logoutNative?(actor: string, provider: string): Promise<void>;
  listRoutines?(agent: string): Promise<readonly AgentRoutineRow[]>;
  routineStatus?(agent: string, name: string): Promise<AgentRoutineStatus>;
  listWorkflows?(agent: string): Promise<readonly AgentWorkflowRun[]>;
  homeCensus?(actor: string): Promise<AgentHomeCensus>;
}

const AVATAR_TONES = ['green', 'lilac', 'peach', 'grey'] as const;

const RUN_STATE_BY_STATE: Readonly<
  Record<string, { runtime: AgentRuntimeState; detail?: string }>
> = {
  idle: { runtime: 'online', detail: '空闲' },
  'in-turn': { runtime: 'online', detail: '进行中' },
  starting: { runtime: 'unknown', detail: '启动中' },
  stopping: { runtime: 'unknown', detail: '停止中' },
  blocked: { runtime: 'online', detail: '已阻塞' },
  'suspected-stuck': { runtime: 'online', detail: '疑似卡住' },
  'not-running': { runtime: 'offline', detail: '未运行' },
  unknown: { runtime: 'unknown' },
};

const HEALTH_NOTES: Readonly<Record<string, string>> = {
  stale: '运行信息已过期',
  unavailable: '暂无运行信息',
  partial: '运行信息不完整',
  'resync-required': '运行信息待同步',
};

function runStateFactsOf(
  runState: unknown,
): AgentRunStateFacts {
  if (!runState || typeof runState !== 'object') {
    return { runtime: 'unknown' };
  }
  const shape = runState as {
    state?: unknown;
    certainty?: unknown;
    observationHealth?: unknown;
  };
  const healthNote =
    typeof shape.observationHealth === 'string'
      ? HEALTH_NOTES[shape.observationHealth]
      : undefined;
  if (healthNote) {
    return { runtime: 'unknown', runtimeDetail: healthNote };
  }
  if (shape.certainty === 'unknown') {
    return { runtime: 'unknown' };
  }
  const mapped =
    typeof shape.state === 'string' ? RUN_STATE_BY_STATE[shape.state] : undefined;
  if (!mapped) {
    return { runtime: 'unknown' };
  }
  if (mapped.runtime === 'unknown') {
    return { runtime: 'unknown', runtimeDetail: mapped.detail };
  }
  const detail =
    shape.certainty === 'inferred' && mapped.detail
      ? `${mapped.detail}（推断）`
      : mapped.detail;
  return { runtime: mapped.runtime, runtimeDetail: detail,
    ...(shape.certainty === 'observed' && shape.observationHealth === 'current' ? {
      summaryState: shape.state === 'idle' || shape.state === 'in-turn' ? 'normal' as const : 'abnormal' as const,
    } : {}) };
}

function asString(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function requireArray(value: unknown): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new Error('Agent 列表格式不正确');
  }
  return value;
}

function avatarToneOf(actor: string): ManagedAgentView['avatarTone'] {
  let sum = 0;
  for (const character of actor) {
    sum += character.codePointAt(0) ?? 0;
  }
  return AVATAR_TONES[sum % AVATAR_TONES.length];
}

function rowOfAgentEntity(raw: unknown): AgentDirectoryRow {
  const row = raw as Record<string, unknown>;
  const actor = asString(row.actor, '');
  if (!actor) {
    throw new AgentRequestError(
      'AGENT_RESPONSE_INVALID',
      'agent 实体缺少 actor',
      'dispatched',
      null,
      false,
    );
  }
  const uri = asString(row.uri, '');
  return {
    actor,
    agentId: asNullableString(row.agentId),
    uri,
    displayName: asString(row.displayName, '').trim() || actor,
    owner: asString(row.owner, '') || '未提供',
    host: asString(row.machine, '') || '未提供',
    provider: asNullableString(row.provider),
    model: asNullableString(row.model),
    harness: asNullableString(row.harness),
    preferredHarness: asNullableString(row.preferredHarness),
    capabilities: [],
    modelConfigurationRequired: (row.capabilities as Record<string, unknown> | null)?.modelConfigurationRequired === true,
    runState: runStateFactsOf(row.runState),
    ...(row.startupFailure && typeof row.startupFailure === 'object'
      && typeof (row.startupFailure as { operationId?: unknown }).operationId === 'string'
      ? { startupFailure: {
        operationId: (row.startupFailure as { operationId: string }).operationId,
        message: String((row.startupFailure as { message?: unknown }).message ?? '').includes('configured Pi model')
          ? '启动失败：未找到所选模型，请修改模型配置后重新启动。'
          : '启动失败：运行程序未就绪，请检查配置后重新启动。',
      } } : {}),
    cwd: asNullableString(row.cwd),
    homeMtimeMs: activityHintNumber(row.activityHints, 'agentHomeMtimeMs'),
    createdAtMs: activityHintNumber(row.activityHints, 'createdAtMs'),
    hasLastSessionId: activityHintBoolean(row.activityHints, 'hasLastSessionId'),
    lastActiveAtMs: asNullableNumber(row.lastActiveAtMs),
  };
}

function activityHintNumber(hints: unknown, key: string): number | null {
  if (!hints || typeof hints !== 'object') {
    return null;
  }
  return asNullableNumber((hints as Record<string, unknown>)[key]);
}

function activityHintBoolean(hints: unknown, key: string): boolean | null {
  if (!hints || typeof hints !== 'object') {
    return null;
  }
  const value = (hints as Record<string, unknown>)[key];
  return typeof value === 'boolean' ? value : null;
}

function asNullableNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

function routineRowOf(raw: unknown): AgentRoutineRow {
  const row = raw as Record<string, unknown>;
  const name = asString(row.name, '');
  if (!name) {
    throw new Error('routine 行缺少名称');
  }
  return {
    name,
    owner: asNullableString(row.owner),
    actor: asString(row.actor, ''),
    actorOwnership: asNullableString(row.actorOwnership),
    enabled: row.enabled === true,
    mode: asNullableString(row.mode),
    role: asNullableString(row.role),
    nextDueMs: asNullableNumber(row.nextDueMs),
    lastOutcome: asNullableString(row.lastOutcome),
    lastDispatchAtMs: asNullableNumber(row.lastDispatchAtMs),
    sourceErrorStreak: typeof row.sourceErrorStreak === 'number' ? row.sourceErrorStreak : 0,
    missedPeriods: asNullableNumber(row.missedPeriods),
    quarantined: row.quarantined === true,
    quarantineReason: asNullableString(row.quarantineReason),
    registrationId: asNullableString(row.registrationId),
    readable: row.readable === true,
    inFlightCount: Array.isArray(row.inFlight) ? row.inFlight.length : 0,
    launch: launchOf(row.launch),
  };
}

function launchOf(value: unknown): AgentRoutineLaunch | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  const row = value as Record<string, unknown>;
  return {
    tier: asNullableString(row.tier),
    harness: asNullableString(row.harness),
    provider: asNullableString(row.provider),
    model: asNullableString(row.model),
  };
}

function routineStatusOf(raw: unknown): AgentRoutineStatus {
  const base = routineRowOf(raw);
  const row = raw as Record<string, unknown>;
  const outcomes = Array.isArray(row.outcomes)
    ? row.outcomes.filter((entry): entry is string => typeof entry === 'string')
    : [];
  const events = Array.isArray(row.scheduleEvents) ? row.scheduleEvents : [];
  return {
    ...base,
    outcomes,
    scheduleEvents: events.map((rawEvent) => {
      const event = rawEvent as Record<string, unknown>;
      return {
        routine: asString(event.routine, base.name),
        slotMs: asNullableNumber(event.slot_ms),
        disposition: asNullableString(event.disposition),
        count: asNullableNumber(event.count),
        reason: asNullableString(event.reason),
        recordedAtMs: asNullableNumber(event.recorded_at_ms),
      };
    }),
  };
}

function workflowRunOf(raw: unknown): AgentWorkflowRun {
  const row = raw as Record<string, unknown>;
  const runId = asString(row.runId, '');
  if (!runId) {
    throw new Error('workflow 行缺少 runId');
  }
  const currentNode =
    row.currentNode && typeof row.currentNode === 'object'
      ? (() => {
          const node = row.currentNode as Record<string, unknown>;
          const nodeId = asString(node.nodeId, '');
          if (!nodeId) {
            return null;
          }
          return { nodeId, state: asString(node.state, 'unknown') };
        })()
      : null;
  return {
    runId,
    graphId: asNullableString(row.graphId),
    name: asNullableString(row.name),
    state: asString(row.state, 'unknown'),
    currentNode,
    createdAtMs: asNullableNumber(row.createdAtMs),
    closedAtMs: asNullableNumber(row.closedAtMs),
    onFailure: asNullableString(row.onFailure),
    sender: asNullableString(row.sender),
  };
}

function censusRowOf(raw: unknown): AgentHomeCensusRow {
  const row = raw as Record<string, unknown>;
  const actor = asString(row.actor, '');
  if (!actor) {
    throw new Error('census 行缺少 actor');
  }
  const mode = row.mode === 'p2' || row.mode === 'legacy' ? row.mode : null;
  return {
    actor,
    uri: asNullableString(row.uri),
    homeProvisioned: row.homeProvisioned === true,
    explicitConfig: row.explicitConfig === true,
    configInsideHome: row.configInsideHome === true,
    cwdInsideHome: row.cwdInsideHome === true,
    residentOrDsh: row.residentOrDsh === true,
    transferReceived: row.transferReceived === true,
    mode,
    legacyReason: asNullableString(row.legacyReason),
    harness: asNullableString(row.harness),
    lastHarness: asNullableString(row.lastHarness),
  };
}

function providerDefaultOf(value: unknown): AgentCreateOutcome['providerDefault'] {
  if (value === null || value === undefined) {
    return { captured: false, provider: null };
  }
  if (typeof value === 'string') {
    return { captured: false, provider: value };
  }
  if (typeof value === 'object') {
    const provider = (value as { provider?: unknown }).provider;
    return {
      captured: true,
      provider: typeof provider === 'string' ? provider : null,
    };
  }
  throw new AgentRequestError(
    'AGENT_RESPONSE_INVALID',
    'providerDefault 格式不正确',
    'dispatched',
    null,
    false,
  );
}

function nativeFileOf(raw: unknown): AgentNativeFileWrite {
  const row = raw as Record<string, unknown>;
  const path = asString(row.path, '');
  if (!path) {
    throw new Error('原生凭据写入回执格式不正确');
  }
  return {
    path,
    bytes: typeof row.bytes === 'number' ? row.bytes : 0,
    replaced: row.replaced === true,
    unchanged: row.unchanged === true,
  };
}

export function createAgentsApi(client: AgentsClient, access?: MethodAccess): Required<AgentsApi>;
export function createAgentsApi(client?: AgentsClient, access?: MethodAccess): AgentsApi;
export function createAgentsApi(client?: AgentsClient, access?: MethodAccess): AgentsApi {
  return withPresentHandlers(client, {
    async previewDestroy(name: string) {
      return callAgentMethod('agent.destroy.preview', client?.agent?.destroy?.preview, { name }, access);
    },
    async destroy(operationId: string) {
      return callAgentMethod('agent.destroy', client?.agent?.destroy, { operationId }, access);
    },
    async list() {
      const { agents } = await callAgentMethod('agent.list', client?.agent?.list, {}, access);
      return requireArray(agents).map(rowOfAgentEntity);
    },
    async listGrants(actor) {
      const { grants } = await callAgentMethod('agent.secret.list', client?.agent?.secret?.list, { actor }, access);
      return requireArray(grants).map((raw) => {
        const row = raw as Record<string, unknown>;
        const grantId = asString(row.grantId, '');
        if (!grantId) {
          throw new Error('授权记录格式不正确');
        }
        const source = asString(row.source, '');
        if (source !== 'agent-private' && source !== 'agent-shared' && source !== 'user-provider') {
          throw new Error('授权记录来源未确认');
        }
        return {
          grantId,
          source,
          fieldName: asNullableString(row.fieldName),
          ownerActor: asNullableString(row.ownerActor),
          revision: typeof row.revision === 'number' ? row.revision : 0,
        } satisfies AgentSecretGrantRow;
      });
    },
    async defaultProvider() {
      const { provider } = await callAgentMethod('agent.provider.default', client?.agent?.provider?.default, {}, access);
      if (provider !== null && typeof provider !== 'string') {
        throw new Error('默认服务格式不正确');
      }
      return { ready: true, provider } satisfies ProviderDefaultStatus;
    },
    async create(input: AgentCreateInput) {
      try {
        const result = await callAgentMethod('agent.create', client?.agent?.create, {
          name: input.name,
          ...(input.cwd ? { cwd: input.cwd } : {}),
          preferredHarness: input.preferredHarness,
          ...(input.modelConfiguration ? { modelConfiguration: input.modelConfiguration }
            : input.requireModelConfiguration ? { capabilities: { modelConfigurationRequired: true } }
              : { provider: input.provider, model: input.model }),
        }, access);
        const entity = rowOfAgentEntity(result.agent);
        if (input.modelConfiguration) {
          const expected = input.modelConfiguration;
          const receipt = result.modelConfiguration;
          if (!receipt?.binding || receipt.cleanupPending || receipt.cell.intentMatches !== true
            || receipt.operation?.phase !== 'finalized' || receipt.operation.operationId !== expected.operationId
            || Object.entries(expected.configuration).some(([key, value]) => (receipt.binding!.configuration as unknown as Record<string, unknown>)[key] !== value)) {
            throw new AgentRequestError('MODEL_CONFIGURATION_INCOMPLETE', 'Model configuration receipt is incomplete.', 'dispatched',
              { reason: 'configuration-required', actor: entity.uri, agentId: entity.agentId ?? entity.actor, operationId: expected.operationId });
          }
        }
        return {
          created: result.created === true,
          actor: entity.actor,
          entity,
          providerDefault: providerDefaultOf(result.providerDefault),
          ...(result.modelConfiguration ? { modelConfiguration: result.modelConfiguration } : {}),
        } satisfies AgentCreateOutcome;
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async stop(name: string) {
      try {
        const result = await callAgentMethod('agent.stop', client?.agent?.stop, { name }, access);
        const removed = Array.isArray(result.removed)
          ? result.removed.filter((entry): entry is string => typeof entry === 'string')
          : [];
        return { removed } satisfies AgentStopOutcome;
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async start(name: string, options?: AgentStartOptions) {
      try {
        const result = await callAgentMethod('agent.start', client?.agent?.start, {
          name,
          ...(options?.harness ? { harness: options.harness } : {}),
          ...(options?.provider ? { provider: options.provider } : {}),
          ...(options?.model ? { model: options.model } : {}),
          ...(options?.cwd ? { cwd: options.cwd } : {}),
          ...(options?.approve !== undefined ? { approve: options.approve } : {}),
        }, access);
        return {
          accepted: result.accepted === true,
          running: result.running === true,
          warnings: Array.isArray(result.warnings)
            ? result.warnings.filter((entry): entry is string => typeof entry === 'string')
            : [],
        } satisfies AgentStartOutcome;
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async rename(actor: string, displayName: string) {
      try {
        const result = await callAgentMethod('agent.rename', client?.agent?.rename, {
          name: actor,
          displayName,
        }, access);
        return rowOfAgentEntity(result.agent);
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    ...secretProviderActions(client, access),
    ...modelActions(client, access),
    async loginNative(
      actor: string,
      provider: string,
      files: readonly { readonly filename: string; readonly contentBase64: string }[],
      options?: { readonly replace?: boolean },
    ) {
      try {
        const result = await callAgentMethod(
          'agent.provider.login',
          client?.agent?.provider?.login,
          { actor, provider, files: [...files], ...(options?.replace ? { force: true } : {}) },
          access,
        );
        return {
          actor: asString(result.actor, actor),
          provider: asString(result.provider, provider),
          files: Array.isArray(result.files) ? result.files.map(nativeFileOf) : [],
        } satisfies AgentNativeLoginOutcome;
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async logoutNative(actor: string, provider: string) {
      try {
        await callAgentMethod(
          'agent.provider.logout',
          client?.agent?.provider?.logout,
          { actor, provider },
          access,
        );
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async listRoutines(agent: string) {
      try {
        const { routines } = await callAgentMethod('routine.list', client?.routine?.list, { agent }, access);
        return requireArray(routines).map(routineRowOf);
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async routineStatus(agent: string, name: string) {
      try {
        const row = await callAgentMethod('routine.status', client?.routine?.status, { agent, name }, access);
        return routineStatusOf(row);
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async listWorkflows(agent: string) {
      try {
        const { runs } = await callAgentMethod('workflow.list', client?.workflow?.list, { agent }, access);
        return requireArray(runs).map(workflowRunOf);
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
    async homeCensus(actor: string) {
      try {
        const result = await callAgentMethod('agent.home-census', client?.agent?.homeCensus, { actor }, access);
        const agents = requireArray(result.agents);
        const totals =
          result.totals && typeof result.totals === 'object'
            ? (result.totals as Record<string, unknown>)
            : {};
        return {
          readAtMs: asNullableNumber(result.readAtMs),
          row: agents.length > 0 ? censusRowOf(agents[0]) : null,
          totals: {
            agents: typeof totals.agents === 'number' ? totals.agents : agents.length,
            homeUnprovisioned:
              typeof totals.homeUnprovisioned === 'number' ? totals.homeUnprovisioned : 0,
          },
        } satisfies AgentHomeCensus;
      } catch (error) {
        throw toAgentRequestError(error);
      }
    },
  });
}

const ORG_UNKNOWN = '组织状态未确认';
const CONNECTION_UNKNOWN = '状态未确认';

/** Project one directory row onto the shared agent list/detail view model. */
export function directoryRowToView(row: AgentDirectoryRow): ManagedAgentView {
  return {
    id: row.actor,
    ...(row.agentId ? { agentId: row.agentId } : {}),
    displayName: row.displayName,
    role: '',
    avatarTone: avatarToneOf(row.actor),
    modelServiceId: null,
    channelServiceId: null,
    organizationId: null,
    modelLabel: [row.provider, row.model].filter(Boolean).join(' · ') || '未指定模型',
    capabilities: row.capabilities,
    organizationLabel: ORG_UNKNOWN,
    ownerLabel: row.owner,
    host: row.host,
    availability:
      row.runState.runtime === 'online'
        ? 'available'
        : row.runState.runtime === 'offline'
          ? 'needs-connection'
          : 'unknown',
    model: {
      kind: 'unknown',
      label: CONNECTION_UNKNOWN,
      sourceLabel: CONNECTION_UNKNOWN,
    },
    channel: {
      kind: 'unknown',
      label: CONNECTION_UNKNOWN,
      sourceLabel: CONNECTION_UNKNOWN,
    },
    activity: {
      homePath: row.cwd,
      homeMtimeMs: row.homeMtimeMs,
      lastActiveAtMs: row.lastActiveAtMs,
    },
  };
}

/** Project one directory row onto the manage-panel extension view model. */
export function directoryRowToExtension(row: AgentDirectoryRow): AgentExtensionView {
  return {
    runtime: row.runState.runtime,
    runtimeDetail: row.runState.runtimeDetail,
    providerGrants: [],
    capabilityGrants: [],
    preset: false,
    harness: row.harness ?? undefined,
    model: row.model ?? undefined,
    preferredHarness: row.preferredHarness ?? undefined,
  };
}

/**
 * Map `agent.secret.list` rows to provider grants. user-provider 行是唯一
 * 有 provider 证据的模型凭据行（来源=我的秘钥授权）；agent 自有行不携带
 * provider 证据，只进「自有秘钥」名称区，不推测为模型凭据行（G4-4）。
 */
export function grantRowsToProviderGrants(
  grants: readonly AgentSecretGrantRow[],
): readonly AgentProviderGrantView[] {
  return grants
    .filter((grant) => grant.source === 'user-provider')
    .map((grant) => ({
      provider: grant.fieldName ?? '凭据字段未确认',
      state: 'ok' as const,
      source: 'user-granted' as const,
      ownerLabel: grant.ownerActor ?? undefined,
      revision: grant.revision,
    }));
}
