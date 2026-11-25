/**
 * Saved user entries use the existing Secret catalog/writer and exact grant IDs.
 * Inventory and grant observation stay separate. Missing times and authentication
 * evidence are not synthesized. Unknown writes are never retried automatically.
 * Custom Pi routes and temporary probes consume generated operator methods.
 * Native Auth remains on its existing independent port; entry deletion is absent.
 * Add rechecks the catalog before writing and refuses existing entry IDs across
 * providers. This read is not an atomic create-only backend guarantee.
 * Custom service creation uses the daemon's atomic createOnly admission.
 */

import { ClientError, accessRefusal, assertMethodAvailable, methodRefusal, type MethodAccess, type MethodRefusal } from '../../client';
import type { GeneratedClient } from '../../client/generated';
import type { AgentSecretListResult, AgentSecretProviderTestResult } from '../../client/generated/types';
import type { NativeAuthApi } from '../agent-provider';
import { SECRET_PROVIDER_ENVIRONMENTS, parseSecretEntryReference, secretEntryReference, secretProviderForEnvironments } from './impl/secret-entries';

export interface SharedGrantRow {
  readonly grantId: string;
  readonly actor: string;
  readonly source: string;
  readonly entryId: string;
  readonly fieldName: string | null;
  readonly environments: readonly string[];
  readonly ownerActor: string | null;
  readonly revision: number;
}

export interface CredentialGrant {
  readonly agent: string;
  readonly agentId: string | null;
  readonly grantId: string;
  readonly provider: string;
  readonly state: string;
}

export type CredentialProtocol = 'openai-completions' | 'anthropic-messages';

export interface CredentialTestRequest {
  readonly provider: string;
  readonly secret: string;
  readonly model: string;
  readonly endpoint?: string;
  readonly protocol?: CredentialProtocol;
}

export interface CredentialEntry {
  readonly keyId: string;
  readonly provider: string;
  readonly endpoint?: string;
  readonly protocol?: CredentialProtocol;
  readonly label: string;
  readonly state: string;
  readonly createdAt: number | null;
  readonly grantsKnown?: boolean;
  readonly lastUsedAt: number | null;
  readonly grants: readonly CredentialGrant[];
}

export type SharedCredentialWrite =
  | { readonly status: 'saved'; readonly message?: string }
  | { readonly status: 'failed'; readonly message: string }
  | { readonly status: 'unknown'; readonly message: string }
  | { readonly status: 'unavailable'; readonly refusal: MethodRefusal };

export type SharedCredentialAction = 'add' | 'replace' | 'delete' | 'revoke';

const ACTION_METHODS: Readonly<Partial<Record<SharedCredentialAction, string>>> = {
  add: 'agent.secret.provider-write',
  replace: 'agent.secret.provider-write',
  revoke: 'agent.secret.revoke',
};

export interface CredentialProbe {
  readonly kind: 'model' | 'blocked';
  readonly status: number | null;
  readonly ok: boolean | null;
  readonly latencyMs: number;
  readonly simulated?: boolean;
  readonly outcome?: AgentSecretProviderTestResult['outcome'];
}

export interface ConfiguredKeyInput {
  readonly provider: string;
  readonly label: string;
  readonly value: string;
  readonly endpoint: string;
}

export function validEndpoint(endpoint: string): boolean {
  try {
    const url = new URL(endpoint);
    return endpoint.length <= 2048 && !/[\s\\"<>]/.test(endpoint)
      && !url.username && !url.password && !endpoint.includes('?') && !endpoint.includes('#')
      && (url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)));
  } catch { return false; }
}

export interface SharedCredentialsApi {
  readonly nativeAuth?: NativeAuthApi;
  listKeys(): Promise<readonly CredentialEntry[]>;
  listSecretGrants(): Promise<readonly SharedGrantRow[]>;
  addKey(provider: string, label: string, value: string): Promise<SharedCredentialWrite>;
  configureKey?(input: ConfiguredKeyInput): Promise<SharedCredentialWrite>;
  testKey?(request: CredentialTestRequest): Promise<CredentialProbe>;
  readonly testProviders?: readonly string[];
  refusal?(action: SharedCredentialAction): MethodRefusal | null;
  replaceKey(keyId: string, value: string): Promise<SharedCredentialWrite>;
  deleteKey?(keyId: string): Promise<SharedCredentialWrite>;
  revokeGrant(agent: string, grantId: string): Promise<SharedCredentialWrite>;
}

function writeOutcome(error: unknown): SharedCredentialWrite {
  const refusal = methodRefusal(error);
  if (refusal) return { status: 'unavailable', refusal };
  if (error instanceof ClientError && error.dispatch === 'not-dispatched') {
    return { status: 'failed', message: '本次操作未派发，未写入任何凭据。' };
  }
  return {
    status: 'unknown',
    message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。',
  };
}

function validKey(value: string): boolean {
  return value.length > 0 && !/[\r\n]/.test(value);
}

export function createNativeAuthApiFromClient(
  client: Pick<GeneratedClient, 'agent'>,
  access?: MethodAccess,
): NativeAuthApi {
  return {
    async list(actor) {
      const params = actor === undefined ? {} : { actor };
      assertMethodAvailable(access, 'agent.provider.auth.list', params);
      const result = await client.agent.provider.auth.list(params);
      return result.records;
    },
  };
}

export function createSharedCredentialsApiFromClient(
  client: Pick<GeneratedClient, 'agent'>,
  access?: MethodAccess,
): SharedCredentialsApi {
  const nativeAuth = createNativeAuthApiFromClient(client, access);
  const configure = typeof client.agent?.secret?.providerConfigure === 'function' ? {
    async configureKey(input: ConfiguredKeyInput): Promise<SharedCredentialWrite> {
      const entryId = input.label.trim();
      const fieldName = 'DEEPSEEK_API_KEY';
      if (input.provider !== 'deepseek' || !validEndpoint(input.endpoint) || !validKey(input.value)
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(entryId)) {
        return { status: 'failed', message: '请检查名称、Key 和服务地址；自定义服务目前仅支持 DeepSeek。' };
      }
      const params = { entryId, fieldName, value: input.value, endpoint: input.endpoint,
        protocol: 'openai-completions' as const, modelProvider: 'deepseek' as const, createOnly: true };
      const refusal = accessRefusal(access, 'agent.secret.provider-configure', params);
      if (refusal) return { status: 'unavailable', refusal };
      try {
        const result = await client.agent.secret.providerConfigure(params);
        const route = result.routes?.[fieldName];
        if (result.entryId !== entryId || !result.fieldNames.includes(fieldName)
          || route?.endpoint !== input.endpoint || route.protocol !== params.protocol || route.vendorId !== params.modelProvider) {
          return { status: 'unknown', message: '服务配置回执与目标不一致，结果未知；请刷新清单核对。' };
        }
        return { status: 'saved', message: 'Key 和服务地址已保存。请选择 Pi 的 deepseek-flash 模型并授权。' };
      } catch (error) {
        if (error instanceof ClientError && error.code === 'INVALID_ARGUMENT') {
          return { status: 'failed', message: '名称已存在或配置无效，未写入 Key。请刷新清单后核对。' };
        }
        return writeOutcome(error);
      }
    },
  } : {};
  const test = typeof client.agent?.secret?.providerTest === 'function' ? {
    testProviders: ['deepseek', 'openai', 'anthropic', 'hyprial'],
    async testKey(request: CredentialTestRequest): Promise<CredentialProbe> {
      assertMethodAvailable(access, 'agent.secret.provider-test', request);
      const result = await client.agent.secret.providerTest(request);
      return { kind: 'model', ok: result.outcome === 'passed', outcome: result.outcome,
        status: result.httpStatus, latencyMs: result.latencyMs };
    },
  } : {};
  return {
    ...configure,
    ...test,
    ...(accessRefusal(access, 'agent.provider.auth.list') === null ? { nativeAuth } : {}),
    async listKeys() {
      assertMethodAvailable(access, 'agent.secret.provider-list', {});
      const result = await client.agent.secret.providerList({});
      return result.entries.flatMap(entry => entry.fieldNames.map(fieldName => {
        const route = entry.routes?.[fieldName];
        if (route !== undefined && (!validEndpoint(route.endpoint) || route.protocol !== 'openai-completions' || route.vendorId !== 'deepseek')) {
          throw new Error('服务配置无法识别，请刷新后核对。');
        }
        return {
        keyId: secretEntryReference(entry.entryId, fieldName),
        provider: route?.vendorId ?? secretProviderForEnvironments([fieldName]),
        ...(route ? { endpoint: route.endpoint, protocol: route.protocol } : {}),
        label: entry.fieldNames.length > 1 ? `${entry.entryId} / ${fieldName}` : entry.entryId,
        state: 'active', createdAt: null, lastUsedAt: null, grants: [], grantsKnown: false,
      }; }));
    },

    async listSecretGrants() {
      assertMethodAvailable(access, 'agent.secret.list', {});
      const grantResult = await client.agent.secret.list({});
      return (grantResult as AgentSecretListResult).grants
        .map(grant => ({
          grantId: grant.grantId,
          actor: grant.actor,
          source: grant.source,
          entryId: grant.entryId,
          fieldName: grant.fieldName,
          environments: grant.environmentNames,
          ownerActor: grant.ownerActor,
          revision: grant.revision,
        }));
    },

    refusal(action) {
      const method = ACTION_METHODS[action];
      return method ? accessRefusal(access, method) : null;
    },

    async addKey(provider, label, value) {
      const entryId = label.trim();
      const fieldName = SECRET_PROVIDER_ENVIRONMENTS[provider];
      if (!fieldName) return { status: 'failed', message: '请选择已支持的模型服务商。' };
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(entryId)) {
        return { status: 'failed', message: '名称以字母或数字开头，仅支持字母、数字、点、下划线和短横线，最多 128 字符。' };
      }
      if (!validKey(value)) return { status: 'failed', message: '请输入有效的 Key。' };
      const refusal = accessRefusal(access, 'agent.secret.provider-write', { entryId, fieldName });
      if (refusal) return { status: 'unavailable', refusal };
      try {
        assertMethodAvailable(access, 'agent.secret.provider-list', {});
        const catalog = await client.agent.secret.providerList({});
        if (catalog.entries.some(entry => entry.entryId === entryId)) {
          return { status: 'failed', message: '该名称已存在，未写入 Key。请使用新名称；如需更新，请在原条目中选择“更换 Key”。' };
        }
      } catch (error) {
        const refusal = methodRefusal(error);
        return refusal ? { status: 'unavailable', refusal }
          : { status: 'failed', message: '无法核对已有名称，未写入 Key。请刷新清单后重试。' };
      }
      return this.replaceKey(secretEntryReference(entryId, fieldName), value);
    },

    async replaceKey(keyId, value) {
      const target = parseSecretEntryReference(keyId);
      if (!target) return { status: 'failed', message: '缺少有效的凭据标识，请刷新清单。' };
      if (!validKey(value)) return { status: 'failed', message: '请输入有效的 Key。' };
      const refusal = accessRefusal(access, 'agent.secret.provider-write', target);
      if (refusal) return { status: 'unavailable', refusal };
      try {
        const result = await client.agent.secret.providerWrite({ ...target, value });
        if (result.entryId !== target.entryId || !result.fieldNames.includes(target.fieldName)) {
          return { status: 'unknown', message: '写入回执与目标不一致，结果未知；已暂停重复提交。' };
        }
        return { status: 'saved', message: 'Key 已保存；已运行的 Agent 需重启后使用。' };
      } catch (error) { return writeOutcome(error); }
    },

    async revokeGrant(actor, grantId) {
      if (!actor.trim() || !grantId.trim()) return { status: 'failed', message: '缺少 Agent 或授权标识。' };
      const params = { actor, grantId };
      const refusal = accessRefusal(access, 'agent.secret.revoke', params);
      if (refusal) return { status: 'unavailable', refusal };
      try {
        const result = await client.agent.secret.revoke(params);
        if (!result.revoked) return { status: 'failed', message: '未找到该授权，请刷新清单后核对。' };
        return { status: 'saved', message: '授权已撤销；已运行的 Agent 需停止后才能清除已加载的 Key。' };
      } catch (error) { return writeOutcome(error); }
    },
  };
}
