/**
 * implG2 (2026-11-02) 决策记录：
 * - 写侧数据层：agent.create / agent.stop 经生成 client，组件不接触传输细节。
 *   错误统一归一为 AgentRequestError（契约 code、dispatch 三态、error.data），
 *   文案映射在本层完成，组件只渲染 tone+text。
 * - dispatch=unknown 一律「结果未知」，不显示成功、不自动重试（retry=never
 *   由生成 client 与传输层保证，本层不再叠加重试）。
 * - providerDefault 三个契约分支如实区分：对象=已捕获凭据信息（captured=true），
 *   字符串=历史 provider 名（不得当作已捕获凭据），null=无。
 * - agent.stop 的 LIFECYCLE_OPERATION_UNSETTLED 表示指令已发出但结果未知，
 *   文案保留「结果未知」语义；空 removed 是合法结果（没有在运行的运行项），
 *   不显示为失败。
 * implG3 (2026-11-02) 决策记录：
 * - agent.start 已从契约/目录/生成 client 移除（implBc rework @ 966709bdab），
 *   本层不再提供任何 start 数据路径；启动入口一律「暂不可用」且零请求。
 * - create/stop 错误文案覆盖合并后契约声明的全部错误码（create 30、stop 48），
 *   映射表 CREATE_DECLARED_ERROR_TEXT / STOP_DECLARED_ERROR_TEXT 导出供
 *   覆盖测试对照 openapi.json 机械核验；未声明 code 才走通用兜底文案。
 *   表结构：SHARED_DECLARED_ERROR_TEXT（两方法共同声明码）+
 *   CREATE_ONLY_ERROR_TEXT / STOP_ONLY_ERROR_TEXT（各自独有声明码）合成。
 *   create 独有四码沿用 implG2 已验收文案；stop 独有码按生命周期 saga 语义
 *   措辞（管理器不可用/超时未结算/领域步骤失败/路由与 harness 步骤失败）。
 * - 结算未知类 code（SUBMIT_OUTCOME_UNKNOWN、LIFECYCLE_OPERATION_UNSETTLED、
 *   LIFECYCLE_OPERATION_TIMEOUT、LIFECYCLE_COMPENSATION_TIMEOUT、
 *   DOMAIN_COMMAND_TIMEOUT、AGENT_DESTROY_SETTLEMENT_UNKNOWN）一律「结果未知」；未送达类
 *   （PORT_CLOSING、PORT_OVERLOADED、SENDER_UNRESOLVED、
 *   ACTOR_*_UNDELIVERED）一律「未执行/未送达」；其余如实「未完成/未执行」，
 *   绝不显示成功。SUBMIT_OUTCOME_UNKNOWN 的 operationId 出现在文案中。
 * implS2 (2026-11-02, second submission) 决策记录：
 * - agent.start 重新进入契约（合并 4235a9d61c），START_DECLARED_ERROR_TEXT
 *   覆盖其全部 66 个声明码：SHARED ∪ CREATE_ONLY（start 复用 create 前置的
 *   四码）∪ STOP_ONLY（start 与 stop 共享同一生命周期 saga 词汇）∪
 *   START_ONLY（前向 launch 家族 14 码 + 启动专属 SUBMIT_OUTCOME_UNKNOWN
 *   改写）。startFailureMessage 的 operationId 文案用「启动受理结果未知」，
 *   不再沿用 stop 的专属前缀；AGENT_ALREADY_RUNNING 的运行绑定（existing）
 *   结构化返回给组件展示。
 * - 读方法（routine.list/status、workflow.list、agent.home-census）的失败文案表与
 *   readFailureMessage 已删除（2026-11-14）：读取它们的运行页签从未挂载，没有消费者；
 *   待有真实页面再按那时的需要重写，不保留无人读取的文案表。
 * - USER_AGENT_IMMUTABLE（2026-11-18）：daemon 为本机 owner 建的用户 agent（代表本人，
 *   无 harness）拒绝管理类操作；该码声明在 start/stop 及 rename、provider、secret 等
 *   管理方法上，这些方法的失败都经本文件的映射表，故只在 SHARED 表加一条文案。
 *   文案按本表惯例写出操作结果（操作未执行）；用"不接受"而非"不支持"，因为后者属于
 *   不可用文案词表，check_gui_unavailable_copy 只许该台账收缩。
 */

import { ClientError, DomainError, MethodRefusedError } from '../../../../client';
import type { DispatchState, RequestDiagnostics } from '../../../../client';
import type { AgentDirectoryRow } from './index';

export interface AgentStartOptions {
  readonly harness?: string | null;
  readonly provider?: string | null;
  readonly model?: string | null;
  readonly cwd?: string | null;
  readonly approve?: boolean;
}

/** One write attempt that did not end in a contract success result. */
export class AgentRequestError extends Error {
  readonly code: string;
  readonly dispatch: DispatchState;
  readonly domain: boolean;
  readonly data: unknown;
  readonly diagnostics: RequestDiagnostics | null;

  constructor(
    code: string,
    message: string,
    dispatch: DispatchState,
    data: unknown = null,
    domain = true,
    diagnostics: RequestDiagnostics | null = null,
  ) {
    super(message);
    this.name = 'AgentRequestError';
    this.code = code;
    this.dispatch = dispatch;
    this.domain = domain;
    this.data = data;
    this.diagnostics = diagnostics;
  }
}

export function isAgentRequestError(error: unknown): error is AgentRequestError {
  return error instanceof AgentRequestError;
}

/** Normalize any client failure into the feature's one error shape. */
export function toAgentRequestError(error: unknown): AgentRequestError {
  if (error instanceof AgentRequestError) {
    return error;
  }
  if (error instanceof MethodRefusedError) {
    return new AgentRequestError(
      error.code,
      error.message,
      error.dispatch,
      { method: error.method },
      false,
    );
  }
  if (error instanceof ClientError) {
    const data = error instanceof DomainError ? error.data : null;
    const domain = error instanceof DomainError;
    return new AgentRequestError(error.code, error.message, error.dispatch, data, domain, error.diagnostics);
  }
  return new AgentRequestError(
    'AGENT_REQUEST_FAILED',
    error instanceof Error ? error.message : String(error),
    'unknown',
    null,
    false,
  );
}

export interface AgentCreateInput {
  readonly name: string;
  readonly org?: string | null;
  readonly cwd?: string | null;
  readonly provider: string | null;
  readonly model: string | null;
  readonly preferredHarness: string | null;
}

export interface AgentCreateOutcome {
  readonly created: boolean;
  readonly orgMembership?: { readonly state: 'pending' | 'approved' | 'unassigned'; readonly org: string };
  readonly actor: string;
  readonly entity: AgentDirectoryRow;
  readonly providerDefault: {
    readonly captured: boolean;
    readonly provider: string | null;
  };
}

export interface AgentStopOutcome {
  readonly removed: readonly string[];
}

/** One `agent.start` success result; running claims only positive evidence. */
export interface AgentStartOutcome {
  readonly accepted: boolean;
  readonly running: boolean;
  readonly warnings: readonly string[];
}

export interface AgentFailureMessage {
  readonly tone: 'error';
  readonly text: string;
  readonly dispatch: DispatchState;
}

const SHARED_DECLARED_ERROR_TEXT: Readonly<Record<string, string>> = {
  INVALID_ARGUMENT: '参数未通过校验。',
  CALLER_NOT_AUTHORIZED: '当前身份无权执行该操作。',
  TARGET_NOT_AUTHORIZED: '目标 Agent 拒绝该操作。',
  AGENT_NOT_FOUND: '未找到该 Agent。',
  AGENT_ERROR: 'Agent 状态异常，操作未完成。',
  DAEMON_ERROR: 'daemon 返回错误，操作未完成。',
  AGENT_EXISTS: '名称已被占用：本机已有同名 Agent。',
  AGENT_ALREADY_RUNNING: '该 Agent 已在运行，请求被拒绝。',
  HARNESS_START_FAILED: 'harness 启动失败，操作未完成。',
  AGENT_DESTROY_SETTLEMENT_UNKNOWN: '销毁结算未确认；结果未知，请核对列表。',
  AGENT_VERSION_CONFLICT: 'Agent 状态代次冲突，操作未完成。',
  INVALID_AGENT_NAME: 'Agent 名称不符合要求，操作未执行。',
  SENDER_UNRESOLVED: '请求方身份未确认，操作未执行。',
  AGENT_HOME_ACCESS_FAILED: 'Agent 工作区访问失败，操作未完成。',
  AGENT_RUNTIME_CONTEXT_STALE: 'Agent 运行环境上下文已过期，操作未完成。',
  AGENT_RUNTIME_LAUNCH_MISMATCH: 'Agent 运行环境与请求不符，操作未完成。',
  AGENT_LAUNCH_IN_PROGRESS: 'Agent 启动正在进行，请稍后核对运行状态。',
  AGENT_MIGRATION_BUSY: 'Agent 迁移进行中，操作未执行。',
  USER_AGENT_IMMUTABLE: '这是代表你本人的用户 Agent，不接受此操作；操作未执行。',
  PORT_CLOSING: 'daemon 正在关闭，请求未执行。',
  PORT_OVERLOADED: 'daemon 过载，请求未执行。',
  DOMAIN_COMMAND_TIMEOUT: '指令处理超时；结果未知，请核对列表或运行状态。',
  ACTOR_STOPPED_UNDELIVERED: '目标进程已停止，指令未送达，操作未执行。',
  ACTOR_RESTARTED_UNDELIVERED: '目标进程正在重启，指令未送达，操作未执行。',
  ACTOR_QUARANTINED_UNDELIVERED: '目标进程已被隔离，指令未送达，操作未执行。',
};

const CREATE_ONLY_ERROR_TEXT: Readonly<Record<string, string>> = {
  AGENT_DEFAULT_ROUTINE_PENDING: 'Agent 已创建；默认例程登记仍在后台进行。',
  AGENT_DEFAULT_ROUTINE_FAILED: '创建已回滚：默认例程登记失败。',
};

const STOP_ONLY_ERROR_TEXT: Readonly<Record<string, string>> = {
  LIFECYCLE_MANAGER_UNAVAILABLE: '生命周期管理器不可用，未执行停止。',
  LIFECYCLE_OPERATION_UNSETTLED:
    '停止指令已发出但结果未知；请核对运行状态，不要盲目重复停止。',
  SUBMIT_OUTCOME_UNKNOWN: '停止受理结果未知；请核对运行状态。',
  DAEMON_START_FAILED: '停止流程未正常完成；请核对运行状态后再决定是否重试。',
  LIFECYCLE_OPERATION_TIMEOUT: '停止操作超时未结算；结果未知，请核对运行状态。',
  LIFECYCLE_COMPENSATION_TIMEOUT: '停止补偿流程超时；结果未知，请核对运行状态。',
  LIFECYCLE_DOMAIN_FAILED: '停止流程中的领域步骤失败，停止未完成。',
  ROUTE_LEASE_STALE: '路由租约已过期，停止未完成。',
  ROUTE_IO_FAILED: '路由 I/O 失败，停止未完成。',
  ROUTE_CLOSED_BEFORE_COMPLETION: '路由在停止完成前关闭，停止未正常结束。',
  TRANSFER_CUSTODY_UNAVAILABLE: '托管转移不可用，停止未完成。',
  AGENT_INCARNATION_MISMATCH: 'Agent 代次不匹配，停止未执行。',
  LIFECYCLE_INCARNATION_UNSUPPORTED: '该 Agent 生命周期形态不支持此停止操作。',
  HARNESS_RUNTIME_CLOSING: '目标 harness 正在关闭，停止未执行。',
  HARNESS_LIFECYCLE_IN_PROGRESS: 'harness 生命周期操作进行中，停止未执行。',
  LIFECYCLE_ATTEMPT_SETTLED: '停止尝试已结算，未发起新的停止。',
  HARNESS_LIFECYCLE_RECEIPT_MISMATCH: 'harness 生命周期回执不匹配，停止未完成。',
  HARNESS_LIFECYCLE_STORE_MISSING: 'harness 生命周期记录缺失，停止未完成。',
  HARNESS_IO_FAILED: 'harness I/O 失败，停止未完成。',
  STALE_HARNESS_IO_COMPLETION: '收到过期的 harness 完成回报，停止未完成。',
  HARNESS_START_TIMEOUT: '补偿性 harness 启动超时，停止未完成。',
  HARNESS_COMPLETION_HANDOFF_FAILED: '停止完成交接失败，请核对运行状态。',
};

export const CREATE_DECLARED_ERROR_TEXT: Readonly<Record<string, string>> = {
  ...SHARED_DECLARED_ERROR_TEXT,
  ...CREATE_ONLY_ERROR_TEXT,
};

export const STOP_DECLARED_ERROR_TEXT: Readonly<Record<string, string>> = {
  ...SHARED_DECLARED_ERROR_TEXT,
  ...STOP_ONLY_ERROR_TEXT,
};

const START_ONLY_ERROR_TEXT: Readonly<Record<string, string>> = {
  HARNESS_BINARY_MISSING: '当前设备未找到所选运行程序，启动未执行。请确认已安装，并检查 daemon 启动时的 PATH。',
  SUBMIT_OUTCOME_UNKNOWN: '启动受理结果未知；请核对运行状态。',
  TRANSFER_STARTS_HELD: '迁移托管期间停止保持，启动未执行。',
  USE_ADAPTER_COMMAND: '该 agent 需经 adapter 启动，本次请求未执行。',
  HARNESS_CWD_MISSING: '工作目录不存在或不是文件夹。',
  HARNESS_ARGS_INVALID: 'harness 参数未通过校验，启动未执行。',
  CLAUDE_CREDENTIAL_MISSING: 'claude 凭据缺失，启动未完成。',
  CODEX_CREDENTIAL_MISSING: 'codex 凭据缺失，启动未完成。',
  PI_P2_LAUNCH_UNTRUSTED: 'pi 项目目录未被信任，启动未完成。',
  AGENT_SECRET_RESOLUTION_FAILED: '启动所需服务秘钥解析失败，启动未完成。',
  AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED: '运行启动托管租约未取得，启动未执行。',
  SHARED_CREDENTIAL_CONFLICT: '共享凭据环境冲突，启动未完成。',
  PROVIDER_UNSAFE_PATH: 'provider 原生目录路径不安全，启动未完成。',
  UNSUPPORTED: '本机不支持该 provider 的原生目录形态，启动未执行。',
  WORKER_PROXY_SETTINGS_INVALID: 'worker 代理设置无效，启动未完成。',
  WORKER_PROXY_URL_INVALID: 'worker 代理 URL 无效，启动未完成。',
  WORKER_PROXY_VENDORS_INVALID: 'worker 代理 vendor 清单无效，启动未完成。',
};

export const START_DECLARED_ERROR_TEXT: Readonly<Record<string, string>> = {
  ...SHARED_DECLARED_ERROR_TEXT,
  ...CREATE_ONLY_ERROR_TEXT,
  ...STOP_ONLY_ERROR_TEXT,
  ...START_ONLY_ERROR_TEXT,
};

const TRANSPORT_ERROR_TEXT: Readonly<Record<string, string>> = {
  REQUEST_ABORTED: '请求已取消，未完成。',
  HTTP_UNAUTHORIZED: '当前身份无权执行该操作。',
  HTTP_FORBIDDEN: '当前身份无权执行该操作。',
  CATALOG_DRIFT: '服务目录已变化，请求被拒绝；请刷新后重试。',
};

const UNKNOWN_OUTCOME_TEXT =
  '请求已发出但结果未知；不会自动重试，请核对列表确认实际状态。';

function declaredErrorText(
  error: AgentRequestError,
  map: Readonly<Record<string, string>>,
  outcomeUnknownLabel: string,
): string | undefined {
  const mapped = map[error.code];
  if (mapped === undefined) {
    return undefined;
  }
  if (error.code === 'INVALID_ARGUMENT' && error.dispatch === 'not-dispatched') {
    return '参数未通过校验，请求未发送。';
  }
  if (error.code === 'SUBMIT_OUTCOME_UNKNOWN') {
    const data = error.data as { operationId?: unknown } | null;
    if (typeof data?.operationId === 'string' && data.operationId !== '') {
      return `${outcomeUnknownLabel}受理结果未知（操作 ${data.operationId}）；请核对运行状态。`;
    }
  }
  return mapped;
}

export function agentFailureMessage(error: unknown): AgentFailureMessage {
  if (!(error instanceof AgentRequestError)) {
    return { tone: 'error', text: UNKNOWN_OUTCOME_TEXT, dispatch: 'unknown' };
  }
  if (error.dispatch === 'unknown') {
    return { tone: 'error', text: UNKNOWN_OUTCOME_TEXT, dispatch: 'unknown' };
  }
  const specific =
    declaredErrorText(error, CREATE_DECLARED_ERROR_TEXT, '停止') ??
    declaredErrorText(error, STOP_DECLARED_ERROR_TEXT, '停止') ??
    TRANSPORT_ERROR_TEXT[error.code];
  if (specific) {
    return { tone: 'error', text: specific, dispatch: error.dispatch };
  }
  return {
    tone: 'error',
    text: `操作未完成（${error.code}）。`,
    dispatch: error.dispatch,
  };
}

/** agent.start 已在运行时的运行绑定（契约 data.existing），供组件展示。 */
export interface AgentRunningBinding {
  readonly actor: string;
  readonly harness: string | null;
  readonly runtime: string | null;
  readonly sessionId: string | null;
  readonly boundAtMs: number | null;
}

export interface AgentStartFailureMessage {
  readonly tone: 'error';
  readonly text: string;
  readonly dispatch: DispatchState;
  readonly runningBinding: AgentRunningBinding | null;
}

function runningBindingOf(data: unknown): AgentRunningBinding | null {
  if (!data || typeof data !== 'object') {
    return null;
  }
  const existing = (data as { existing?: unknown }).existing;
  if (!existing || typeof existing !== 'object') {
    return null;
  }
  const row = existing as Record<string, unknown>;
  const actor = asNonEmptyString(row.actor);
  if (actor === null) {
    return null;
  }
  return {
    actor,
    harness: asNonEmptyString(row.harness),
    runtime: asNonEmptyString(row.runtime),
    sessionId: asNonEmptyString(row.sessionId),
    boundAtMs: typeof row.boundAtMs === 'number' ? row.boundAtMs : null,
  };
}

function asNonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

/**
 * agent.start 的失败文案：声明码专属映射 + AGENT_ALREADY_RUNNING 的运行
 * 绑定结构化透出；dispatch=unknown 一律「结果未知」，绝不自动重试。
 */
export function startFailureMessage(error: unknown): AgentStartFailureMessage {
  if (!(error instanceof AgentRequestError)) {
    return { tone: 'error', text: UNKNOWN_OUTCOME_TEXT, dispatch: 'unknown', runningBinding: null };
  }
  if (error.dispatch === 'unknown') {
    return { tone: 'error', text: UNKNOWN_OUTCOME_TEXT, dispatch: 'unknown', runningBinding: null };
  }
  if (error.code === 'HARNESS_START_FAILED') {
    const detail = (error.data as { lastError?: unknown } | null)?.lastError;
    const modelUnavailable = [error.message, detail].some(value => typeof value === 'string' && value.includes('configured Pi model'));
    return { tone: 'error', text: modelUnavailable
      ? '启动失败：未找到所选模型，请修改模型配置后重新启动。'
      : '启动失败：运行程序未就绪，请检查配置后重新启动。', dispatch: error.dispatch, runningBinding: null };
  }
  const specific =
    declaredErrorText(error, START_DECLARED_ERROR_TEXT, '启动') ??
    TRANSPORT_ERROR_TEXT[error.code];
  const text = specific ?? `启动未完成（${error.code}）。`;
  return {
    tone: 'error',
    text,
    dispatch: error.dispatch,
    runningBinding:
      error.code === 'AGENT_ALREADY_RUNNING' ? runningBindingOf(error.data) : null,
  };
}
