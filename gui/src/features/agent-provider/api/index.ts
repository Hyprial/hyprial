/** MethodSpec-generated business client mapped to the unchanged component view model. */
import { DomainError, HttpError, HttpTransport, TransportError } from '../../../client';
import { createGeneratedClient } from '../../../client/generated';
import type { AgentProviderLoginResult, AgentProviderLogoutResult } from '../../../client/generated/types';

export interface AgentProviderRequest { request(method: string, params: Record<string, unknown>): Promise<unknown> }
export interface AgentOption { readonly actor: string; readonly label: string }
export interface AgentGrant { readonly id: string; readonly source: string; readonly field: string; readonly environments: readonly string[] }
/** Host-owned credential input, not a second response DTO. */
export interface ProviderLoginFile { readonly filename: string; readonly contentBase64: string }
export class ProviderMutationRejected extends Error {
  constructor() { super('操作未执行或已被拒绝'); }
}
export interface AgentProviderApi {
  readonly mockBoundary?: object;
  listAgents(): Promise<readonly AgentOption[]>;
  getDefault(): Promise<string | null>;
  listGrants(actor: string): Promise<readonly AgentGrant[]>;
  login(actor: string, provider: string, files: readonly ProviderLoginFile[]): Promise<void>;
  logout(actor: string, provider: string): Promise<void>;
}
const rejections = new Set(['INVALID_ARGUMENT', 'TARGET_NOT_AUTHORIZED', 'PROVIDER_UNSUPPORTED', 'PROVIDER_CREDENTIAL_EXISTS']);
function success(value: unknown): void {
  if (!value || typeof value !== 'object' || !('ok' in value) || value.ok !== true) {
    if (value && typeof value === 'object' && 'code' in value && rejections.has(String(value.code))) throw new ProviderMutationRejected();
    throw new Error('请求未成功');
  }
}
async function mutation(call: () => Promise<AgentProviderLoginResult | AgentProviderLogoutResult>, actor: string, provider: string) {
  try {
    const data = await call();
    if (data.actor !== actor || data.provider !== provider) throw new Error('操作响应格式不正确');
  } catch (error) {
    if ((error instanceof HttpError || error instanceof TransportError || error instanceof DomainError) &&
        (error.dispatch === 'not-dispatched' || (error instanceof DomainError && rejections.has(error.code)))) {
      throw new ProviderMutationRejected();
    }
    throw error;
  }
}
export function createAgentProviderApi(port: AgentProviderRequest): AgentProviderApi {
  const client = createGeneratedClient({ async invoke<T>(method: string, params: unknown): Promise<T> {
    const value = await port.request(method, params as Record<string, unknown>);
    success(value);
    return value as T;
  } });
  return {
    async listAgents() {
      const { agents } = await client.agent.list({});
      if (!Array.isArray(agents)) throw new Error('Agent 列表格式不正确');
      return agents.map(row => {
        if (typeof row.actor !== 'string') throw new Error('Agent 标识缺失');
        return { actor: row.actor, label: row.actor };
      });
    },
    async getDefault() {
      const { provider } = await client.agent.provider.default({});
      if (provider !== null && typeof provider !== 'string') throw new Error('默认服务格式不正确');
      return provider;
    },
    async listGrants(actor) {
      const { grants } = await client.agent.secret.list({ actor });
      if (!Array.isArray(grants)) throw new Error('授权列表格式不正确');
      return grants.map(row => ({ id: row.grantId, source: row.source, field: row.fieldName ?? '', environments: row.environmentNames }));
    },
    async login(actor, provider, files) {
      if (!files.length || files.some(file => !file.filename || !file.contentBase64)) throw new ProviderMutationRejected();
      await mutation(() => client.agent.provider.login({ actor, provider, files: [...files] }), actor, provider);
    },
    async logout(actor, provider) {
      await mutation(() => client.agent.provider.logout({ actor, provider }), actor, provider);
    },
  };
}
export function createHttpAgentProviderApi(basePath = '/api/reference/live', fetcher: typeof fetch = fetch): AgentProviderApi {
  const transport = new HttpTransport({ baseUrl: new URL(basePath, window.location.origin).href,
    token: () => 'same-origin-reference', fetch: fetcher });
  return createAgentProviderApi({ request: (method, params) => transport.invoke(method, params) });
}
