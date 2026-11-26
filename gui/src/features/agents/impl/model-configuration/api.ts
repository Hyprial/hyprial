/** Model configuration consumes the existing generated methods and their CAS receipts. */
import type { AgentModelConfigureParams, AgentModelCancelParams, AgentModelStatusResult, AgentProviderAuthListResult } from '../../../../client/generated/types';
import type { MethodAccess } from '../../../../client';
import { callAgentMethod, type AgentsClient } from '../api/client';
export type ModelConfiguration = AgentModelConfigureParams['configuration'];
export type ModelStatus = AgentModelStatusResult;
export type NativeRecord = AgentProviderAuthListResult['records'][number];
export interface ModelActions {
  modelStatus(name: string, operationId?: string): Promise<ModelStatus>;
  configureModel(params: AgentModelConfigureParams): Promise<ModelStatus>;
  cancelModel(params: AgentModelCancelParams): Promise<ModelStatus>;
  nativeTargets(actor: string): Promise<readonly NativeRecord[]>;
}
export function modelActions(client?: AgentsClient, access?: MethodAccess): ModelActions {
  return {
    modelStatus: (name, operationId) => callAgentMethod('agent.model.status', client?.agent?.model?.status, { name, ...(operationId ? { operationId } : {}) }, access),
    configureModel: params => callAgentMethod('agent.model.configure', client?.agent?.model?.configure, params, access),
    cancelModel: params => callAgentMethod('agent.model.cancel', client?.agent?.model?.cancel, params, access),
    nativeTargets: async actor => (await callAgentMethod('agent.provider.auth.list', client?.agent?.provider?.auth?.list, { actor, targetsVersion: 1 }, access)).records.filter(row => row.actor === actor),
  };
}
export function modelReady(value: ModelStatus | null): boolean {
  return Boolean(value?.binding && !value.cleanupPending && value.cell.intentMatches === true && value.operation?.phase !== 'prepared');
}
export function modelError(error: unknown): string {
  const failure = error as { code?: string; dispatch?: string; data?: { phase?: string; reason?: string } };
  if (failure.dispatch === 'unknown') return '配置结果未知；请刷新核对，不要重复提交。';
  const codes: Record<string, string> = {
    MODEL_CONFIGURATION_CONFLICT: '配置已变化，请刷新后重新核对。',
    MODEL_CONFIGURATION_INCOMPLETE: '上次配置尚未完成；请核对或取消该操作。',
    MODEL_AUTH_UNAVAILABLE: '无法取得所选凭据，请检查登录或授权。',
    MODEL_COMBINATION_UNSUPPORTED: '当前 harness、凭据与运行方式的组合不能使用。',
    MODEL_OPERATION_CAPACITY_EXCEEDED: '配置操作记录已达容量上限。',
    MODEL_CONFIGURATION_INVALID: '模型配置未通过校验。',
  };
  return codes[failure.code ?? ''] ?? '模型配置未完成；请刷新核对。';
}
