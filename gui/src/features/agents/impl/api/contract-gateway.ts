/**
 * implG2 (2026-11-02) 决策记录：
 * - 目录模式（capabilities.agents 存在）下的变更网关：create 走
 *   agent.create（经生成 client），成功后触发 afterCreate（页面用它重读
 *   agent.list，列表权威仍是读取侧，不注入第二份本地实体）。
 * - 本节点只接线创建/启动/停止；契约网关不定义 update/delete，页面据此不渲染
 *   编辑/删除，不注册占位成功。
 * - 2026-11-14（验证修订）：injected client 缺少某个 handler 时对应 api 成员不存在；
 *   没有 api.create 就没有创建网关（返回 undefined），没有 start 与 stop 就没有运行
 *   操作处理函数。页面据此不渲染控件，不把缺失当作被拒绝。
 * - 失败文案统一经 agentFailureMessage：dispatch=unknown 显示「结果未知」，
 *   retry=never 不做任何自动重试。
 * - createRuntimeActionHandler 供管理面板 start/stop 使用：返回 tone+text，
 *   dispatched 表示该次尝试已发出（成功或结果未知），页面据此决定是否重读；
 *   not-dispatched 的拒绝不触发重读。
 * - 2026-11-14：发出前被拒绝（methodRefusal）的 create 返回 refused，start/stop 的
 *   outcome 带 refusal 且 text 为空；页面据此发出通知，不把拒绝当作失败文案。
 * implS2 (2026-11-02, second submission) 决策记录：
 * - agent.start 随契约合并重新接线：start 走 api.start（生成 client，
 *   retry=never），成功按 accepted/running 如实分层文案——running 只在有
 *   正面运行证据时成立；warnings 原样透出；失败经 startFailureMessage
 *   （全部声明码专属文案），AGENT_ALREADY_RUNNING 附运行绑定结构化数据；
 *   dispatch=unknown 显示「结果未知」，绝不自动重试，dispatched 才触发重读。
 */

import type {
  AgentDraft,
  AgentMutationGateway,
  AgentMutationResult,
} from '../AgentMutationGateway';
import {
  agentFailureMessage,
  startFailureMessage,
  toAgentRequestError,
  type AgentRunningBinding,
} from './agent-writes';
import { methodRefusal, type MethodRefusal, type RequestDiagnostics } from '../../../../client';
import type { AgentCreateOutcome } from './agent-writes';
import { directoryRowToView, type AgentStartOptions, type AgentsApi } from './index';

export interface ContractGatewayOptions {
  readonly api: AgentsApi;
  /** Called after any dispatched create attempt (created, reused or unknown). */
  readonly afterCreate?: () => void;
}

export function createContractAgentGateway(
  options: ContractGatewayOptions & { readonly api: Required<AgentsApi> },
): AgentMutationGateway;
export function createContractAgentGateway(
  options: ContractGatewayOptions,
): AgentMutationGateway | undefined;
export function createContractAgentGateway(
  options: ContractGatewayOptions,
): AgentMutationGateway | undefined {
  const { api, afterCreate } = options;
  const createAgent = api.create?.bind(api);
  if (createAgent === undefined) return undefined;
  return {
    async create(draft: AgentDraft): Promise<AgentMutationResult> {
      let outcome: AgentCreateOutcome;
      try {
        outcome = await createAgent({
          name: draft.displayName,
          ...(draft.cwd ? { cwd: draft.cwd } : {}),
          provider: draft.provider ?? null,
          model: draft.model ?? null,
          preferredHarness: draft.preferredHarness ?? null,
        });
      } catch (error) {
        const refusal = methodRefusal(error);
        if (refusal) {
          return { kind: 'refused', refusal };
        }
        const failure = toAgentRequestError(error);
        const message = agentFailureMessage(failure);
        if (message.dispatch !== 'not-dispatched') {
          afterCreate?.();
        }
        return message.dispatch === 'unknown'
          ? { kind: 'unknown', message: message.text, code: failure.code, diagnostics: failure.diagnostics }
          : { kind: 'failed', message: message.text, code: failure.code, diagnostics: failure.diagnostics };
      }
      afterCreate?.();
      return {
        kind: 'created',
        agent: directoryRowToView(outcome.entity),
        actorUri: outcome.entity.uri,
        created: outcome.created,
        notice: outcome.orgMembership?.state === 'pending'
          ? 'Agent 已创建，组织关联结果未知，请在组织详情核对；本机启动不受影响。'
          : outcome.orgMembership?.state === 'unassigned'
            ? 'Agent 已创建，尚未关联组织；可在组织详情中关联，本机启动不受影响。'
          : outcome.created ? undefined : '已复用现有 Agent。',
      };
    },
  };
}

export type RuntimeActionKind = 'start' | 'stop';

export interface RuntimeActionOutcome {
  readonly tone: 'status' | 'error';
  readonly text: string;
  readonly dispatched: boolean;
  readonly awaitingObservation?: boolean;
  readonly code?: string;
  readonly diagnostics?: RequestDiagnostics | null;
  /** Set when the call was refused before dispatch; text is then empty and the page notifies. */
  readonly refusal?: MethodRefusal;
  /** agent.start 的非致命 launch 警告（result.warnings）原样透出。 */
  readonly warnings?: readonly string[];
  /** AGENT_ALREADY_RUNNING 时的运行绑定（error.data.existing）。 */
  readonly runningBinding?: AgentRunningBinding | null;
}

export type RuntimeActionHandler = (
  kind: RuntimeActionKind,
  actor: string,
  options?: AgentStartOptions,
) => Promise<RuntimeActionOutcome>;

export function createRuntimeActionHandler(
  api: Required<AgentsApi>,
  afterSettled?: () => void,
): RuntimeActionHandler;
export function createRuntimeActionHandler(
  api: AgentsApi,
  afterSettled?: () => void,
): RuntimeActionHandler | undefined;
export function createRuntimeActionHandler(
  api: AgentsApi,
  afterSettled?: () => void,
): RuntimeActionHandler | undefined {
  const startAgent = api.start?.bind(api);
  const stopAgent = api.stop?.bind(api);
  if (startAgent === undefined || stopAgent === undefined) return undefined;
  return async (kind, actor, options) => {
    if (kind === 'start') {
      try {
        const outcome = await startAgent(actor, options);
        afterSettled?.();
        return {
          tone: outcome.accepted ? 'status' : 'error',
          text: outcome.accepted
            ? outcome.running
              ? '启动已受理，运行中。'
              : '启动已受理；尚未观察到运行状态，请稍后核对。'
            : 'daemon 未受理启动；请核对运行状态，不要盲目重复启动。',
          dispatched: true,
          awaitingObservation: outcome.accepted,
          warnings: outcome.warnings,
        };
      } catch (error) {
        const refusal = methodRefusal(error);
        if (refusal) {
          return { tone: 'error', text: '', dispatched: false, refusal };
        }
        const failure = toAgentRequestError(error);
        const message = startFailureMessage(failure);
        if (message.dispatch !== 'not-dispatched') {
          afterSettled?.();
        }
        return {
          tone: 'error',
          text: message.text,
          dispatched: message.dispatch !== 'not-dispatched',
          awaitingObservation: message.dispatch === 'unknown' || failure.code === 'AGENT_LAUNCH_IN_PROGRESS',
          runningBinding: message.runningBinding,
          code: failure.code,
          diagnostics: failure.diagnostics,
        };
      }
    }
    try {
      const outcome = await stopAgent(actor);
      afterSettled?.();
      return outcome.removed.length === 0
        ? {
            tone: 'status',
            text: '该 Agent 当前没有需要停止的运行项。',
            dispatched: true,
            awaitingObservation: false,
          }
        : {
            tone: 'status',
            text: `停止已完成，已移除 ${outcome.removed.length} 个运行项。`,
            dispatched: true,
            awaitingObservation: false,
          };
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) {
        return { tone: 'error', text: '', dispatched: false, refusal };
      }
      const failure = toAgentRequestError(error);
      const message = agentFailureMessage(failure);
      const dispatched = message.dispatch !== 'not-dispatched';
      if (dispatched) {
        afterSettled?.();
      }
      return { tone: 'error', text: message.text, dispatched,
        awaitingObservation: message.dispatch === 'unknown'
          || ['LIFECYCLE_OPERATION_UNSETTLED', 'SUBMIT_OUTCOME_UNKNOWN', 'DOMAIN_COMMAND_TIMEOUT'].includes(failure.code),
        code: failure.code, diagnostics: failure.diagnostics };
    }
  };
}
