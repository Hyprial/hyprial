/**
 * Decision record 2026-11-05, PAC workflow-65bea2f2-e55c-530b-863f-2ba95b2e1054;
 * S2d cleanup 2026-11-22:
 *
 * - This adapter consumes only GeneratedClient.agent.secret.providerWrite and
 *   agent.secret.list. It creates no second transport or DTO authority and never
 *   uses provider.login.
 * - Ordinary App composition uses the DeepSeek provider target (deepseek-official/key).
 *   The generic adapter still accepts an explicit target for narrow callers and tests.
 * - Any existing grant that references the target blocks replacement before
 *   providerWrite is dispatched.
 * - MethodAccess refusal is unavailable and carries the machine refusal only
 *   ({ code, source }), never a sentence: the consumer notifies when the user
 *   operates the control. Local validation, a failed grant inventory read and
 *   any not-dispatched client failure are failed; dispatched, lost, post-dispatch
 *   cancellation, malformed receipts and untyped failures are unknown.
 * - Unknown results are never retried here. The key is never written to browser
 *   storage, logs, or the URL, and the adapter drops its reference after settle.
 * - First-run fixed Squire preparation and browser identity tracking were
 *   removed in S2d; first-run now uses ordinary agent configuration via
 *   configureAgent.
 */
import {
  ClientError, accessRefusal, methodRefusal,
  type MethodAccess, type MethodRefusal,
} from '../../../client';
import type { GeneratedClient } from '../../../client/generated';
import type {
  AgentSecretListResult,
  AgentSecretProviderWriteParams,
  AgentSecretProviderWriteResult,
} from '../../../client/generated/types';
import {
  unknownModelKeySaveResult,
  type FirstRunActions,
  type ModelKeySaveResult,
} from '../../onboarding';

export const DEEPSEEK_MODEL_KEY_TARGET = Object.freeze({
  entryId: 'deepseek-official',
  fieldName: 'key',
});

export interface ModelKeySaveTarget {
  readonly entryId: string;
  readonly fieldName: string;
}

export interface ModelKeySaveOptions {
  readonly signal?: AbortSignal;
}

export interface ModelKeySaveAdapter {
  readonly target: ModelKeySaveTarget;
  save(key: string, options?: ModelKeySaveOptions): Promise<ModelKeySaveResult>;
}

type ModelKeyClient = Pick<GeneratedClient, 'agent'>;

function targetGrants(result: AgentSecretListResult) {
  return result.grants.filter(grant => grant.source === 'user-provider'
    && grant.entryId === DEEPSEEK_MODEL_KEY_TARGET.entryId
    && grant.fieldName === DEEPSEEK_MODEL_KEY_TARGET.fieldName);
}

export function createConnectedModelKeyActions(
  client: ModelKeyClient,
  access?: MethodAccess,
): FirstRunActions {
  const writer = createModelKeySaveAdapter(client, DEEPSEEK_MODEL_KEY_TARGET, access);
  return Object.freeze({
    async saveModelKey(key: string) {
      const listRefusal = accessRefusal(access, 'agent.secret.list', {});
      if (listRefusal) return unavailable(listRefusal);
      let inventory: AgentSecretListResult;
      try {
        inventory = await client.agent.secret.list({});
      } catch (error) {
        const refusal = methodRefusal(error);
        return refusal ? unavailable(refusal) : failed('无法确认现有授权，未发起凭证写入。');
      }
      if (targetGrants(inventory).length > 0) {
        return failed('该 DeepSeek 凭证已被 Agent 授权引用；为避免静默覆盖，未发起写入。');
      }
      return writer.save(key);
    },
  });
}

function failed(message: string): ModelKeySaveResult {
  return { status: 'failed', message };
}

function unavailable(refusal: MethodRefusal): ModelKeySaveResult {
  return { status: 'unavailable', refusal };
}

function validTarget(target: ModelKeySaveTarget): boolean {
  return target.entryId.length > 0
    && target.fieldName.length > 0
    && !/[\r\n]/.test(target.entryId)
    && !/[\r\n]/.test(target.fieldName);
}

function validKey(value: string): boolean {
  return value.length > 0 && !/[\r\n]/.test(value);
}

function validReceipt(value: unknown, target: ModelKeySaveTarget): value is AgentSecretProviderWriteResult {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return row.ok === true
    && row.entryId === target.entryId
    && Array.isArray(row.fieldNames)
    && row.fieldNames.length > 0
    && row.fieldNames.every(field => typeof field === 'string')
    && row.fieldNames.includes(target.fieldName);
}

export function createModelKeySaveAdapter(
  client: ModelKeyClient,
  target: ModelKeySaveTarget,
  access?: MethodAccess,
): ModelKeySaveAdapter {
  return {
    target,
    async save(key, options = {}) {
      const value = key.trim();
      if (!validTarget(target)) return failed('凭证保存目标未配置，未发起保存。');
      if (!validKey(value)) return failed('请输入有效的 Key 后再保存。');
      if (options.signal?.aborted) return failed('保存已取消，未发起写入。');

      let refusal: MethodRefusal | null;
      try {
        refusal = accessRefusal(access, 'agent.secret.provider-write', {
          entryId: target.entryId,
          fieldName: target.fieldName,
        });
      } catch {
        return failed('本次保存未派发，未写入凭证。');
      }
      if (refusal) return unavailable(refusal);

      const params: AgentSecretProviderWriteParams = {
        entryId: target.entryId,
        fieldName: target.fieldName,
        value,
      };
      try {
        const result = await client.agent.secret.providerWrite(
          params,
          options.signal ? { signal: options.signal } : undefined,
        );
        if (!validReceipt(result, target)) return unknownModelKeySaveResult();
        return { status: 'saved' };
      } catch (error) {
        const refused = methodRefusal(error);
        if (refused) return unavailable(refused);
        if (error instanceof ClientError && error.dispatch === 'not-dispatched') {
          return failed('本次保存未派发，未写入凭证。');
        }
        return unknownModelKeySaveResult();
      } finally {
        params.value = '';
      }
    },
  };
}
