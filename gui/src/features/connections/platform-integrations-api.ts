/**
 * Decision record 2026-11-10 (平台接入).
 *
 * - 用户面模块叫「平台接入」；adapter 是技术术语，channel 不用于外部平台。
 * - 目标调用面全部用 provider-neutral 的 daemon 方法，不允许 `lark.*`：
 *   adapter.list/status/start/stop/reload/pins/pin/unpin，
 *   adapter.register、adapter.onboard.begin/operation/cancel、adapter.authorization.*、
 *   adapter.identities.*、adapter.routes.*、management.adapter.remove。
 * - 删除走 management.adapter.remove.preview / remove；确认绑定 daemon operationId。
 *   本文件的 live adapter 对未接方法 fail-close，不伪造成功：未接的方法在端口上不存在
 *   （例如 remove/removePreview），界面据此不渲染对应控件。
 * - 被目录拒绝（未派发）的调用不产生说明文字：adapter 抛出 MethodRefusedError，capabilities
 *   只带机器码 { available, code, source }；页面在用户操作时发通知。只支持飞书，不列未接入的平台。
 * - 模拟操作只返回 simulated 标记，不产生外部授权 URL；读取一次完成模拟确认。
 *   权限数组对齐 scope_inventory 的能力 ID，不能填入原始平台 scope。
 * - 身份同步沿用 observe_identity：保留既有绑定、来源和首次发现时间，不删除历史记录。
 *   identitiesRecorded 统计本次观测，不代表存储总行数；采集不生成 owner 或 unionId。
 */

import { ClientError, accessRefusal, assertMethodAvailable, type MethodAccess, type MethodRefusal } from '../../client';
import type { GeneratedClient } from '../../client/generated';
export type PlatformProvider = 'lark';

export interface PlatformProviderOption {
  readonly id: PlatformProvider;
  readonly label: string;
}

export const PLATFORM_PROVIDERS: readonly PlatformProviderOption[] = [
  { id: 'lark', label: '飞书' },
] as const;

export interface AdapterAuthorization {
  readonly adapter: string;
  readonly appId: string | null;
  readonly state: 'authorized' | 'unauthorized' | 'unconfigured' | 'unconfirmed';
  readonly capabilities: readonly string[];
  readonly missingCapabilities: readonly string[];
  readonly checkedAt: number | null;
  readonly reason?: string;
}

export interface AdapterIdentityState {
  readonly state: 'synced' | 'not-synced' | 'unconfirmed';
  readonly count: number | null;
}

/** Closed identity taxonomies from `lark/state/records.py`. */
export type AdapterIdentityKind = 'user' | 'bot' | 'app';
export type AdapterIdentityStanding = 'observed' | 'verified';

/** One recorded platform identity (`adapter.identities.list`). */
export interface AdapterIdentityRow {
  readonly kind: AdapterIdentityKind;
  readonly platformId: string;
  readonly displayName: string | null;
  readonly unionId: string | null;
  readonly standing: AdapterIdentityStanding;
  readonly source: string;
  readonly firstSeenMs: number | null;
  readonly lastSeenMs: number | null;
}

/** Recorded identities returned by the daemon. */
export interface AdapterIdentityList {
  readonly adapter: string;
  readonly count: number;
  readonly identities: readonly AdapterIdentityRow[];
}

/** The daemon preserves partial collection and the identities already stored. */
export interface AdapterIdentitySyncReport {
  readonly adapter: string;
  readonly ok: boolean;
  readonly appId: string;
  readonly bot: { readonly openId: string; readonly appName: string | null };
  readonly chats: readonly {
    readonly chatId: string;
    readonly name: string | null;
    readonly memberTotal: number | null;
    readonly membersRecorded: number;
    readonly complete: boolean;
  }[];
  readonly chatsComplete: boolean;
  readonly identitiesRecorded: number;
  readonly errors: readonly string[];
}

export interface PlatformIntegration {
  readonly name: string;
  readonly provider: string;
  readonly status: string;
  readonly configured: boolean;
  readonly desired: boolean;
  readonly processRunning: boolean;
  readonly online: boolean;
  readonly error?: string;
  readonly boundAgent: string | null;
  readonly authorization: AdapterAuthorization;
  readonly identities: AdapterIdentityState;
}

export type AdapterOperationStatus =
  | 'starting'
  | 'awaiting_user'
  | 'checking'
  | 'succeeded'
  | 'denied'
  | 'expired'
  | 'cancelled'
  | 'failed'
  | 'unknown';

export interface AdapterOperation {
  readonly operationId: string;
  readonly adapter: string;
  readonly provider: string;
  readonly kind: 'add' | 'authorization';
  readonly status: AdapterOperationStatus;
  readonly authorization?: AdapterAuthorization;
  readonly verificationUrl?: string;
  readonly expiresAt?: number;
  readonly errorCode?: string;
  readonly simulated?: boolean;
  readonly configured?: boolean;
  readonly reloaded?: boolean;
  readonly remoteOutcomeUnknown?: boolean;
  readonly nextAction?: 'none' | 'wait' | 'inspect' | 'reload';
}

export interface AddPlatformInput {
  readonly provider: PlatformProvider;
  readonly name: string;
  readonly mode: 'create' | 'adopt';
  readonly appId?: string;
  readonly secret?: string;
}

export interface RemovePreview {
  readonly operationId?: string;
  readonly expiresAt?: number;
  readonly routes?: readonly string[];
  readonly credentialRemoved?: boolean;
  readonly name: string;
  readonly provider: string;
  readonly boundAgent: string | null;
  readonly online: boolean;
  readonly processRunning: boolean;
}

export interface RegisterPlatformInput {
  readonly provider: PlatformProvider;
  readonly name: string;
  readonly appId: string;
  readonly secret: string;
}

export interface AdapterRegistration {
  readonly ok: true;
  readonly adapter: { readonly provider: 'lark'; readonly name: string; readonly appId: string };
  readonly configured: true;
  readonly reloaded: boolean;
  readonly nextAction: 'none' | 'reload';
}

export interface ActionCapability {
  readonly available: boolean;
  readonly code?: string;
  readonly source?: string;
}

export function capabilityRefusal(capability: ActionCapability | undefined): MethodRefusal | null {
  if (!capability || capability.available) return null;
  return {
    code: capability.code ?? 'METHOD_NOT_AVAILABLE',
    ...(capability.source === undefined ? {} : { source: capability.source }),
  };
}

export interface PlatformIntegrationCapabilities {
  readonly add: ActionCapability;
  readonly register?: ActionCapability;
  readonly onboard?: ActionCapability;
  readonly authorization: ActionCapability;
  readonly identities: ActionCapability;
}

export interface PlatformIntegrationsApi {
  readonly capabilities: PlatformIntegrationCapabilities;
  list(): Promise<readonly PlatformIntegration[]>;
  start(name: string): Promise<void>;
  stop(name: string): Promise<void>;
  reload(): Promise<void>;
  authorizationStatus?(name: string): Promise<AdapterAuthorization>;
  register?(input: RegisterPlatformInput): Promise<AdapterRegistration>;
  beginAdd?(input: AddPlatformInput): Promise<AdapterOperation>;
  addOperation?(operationId: string): Promise<AdapterOperation>;
  currentAdd?(name: string): Promise<AdapterOperation | null>;
  cancelAdd?(operationId: string): Promise<AdapterOperation>;
  beginAuthorization?(name: string): Promise<AdapterOperation>;
  currentAuthorization?(name: string): Promise<AdapterOperation | null>;
  authorizationOperation?(operationId: string): Promise<AdapterOperation>;
  cancelAuthorization?(operationId: string): Promise<AdapterOperation>;
  syncIdentities?(name: string): Promise<AdapterIdentitySyncReport>;
  listIdentities?(name: string): Promise<AdapterIdentityList>;
  removePreview?(name: string): Promise<RemovePreview>;
  remove?(name: string, expected: RemovePreview): Promise<void>;
}

function capabilityOf(access: MethodAccess | undefined, method: string): ActionCapability {
  const refusal = accessRefusal(access, method);
  return refusal ? { available: false, code: refusal.code, source: method } : { available: true };
}

function unconfirmedAuthorization(name: string): AdapterAuthorization {
  return {
    adapter: name,
    appId: null,
    state: 'unconfirmed',
    capabilities: [],
    missingCapabilities: [],
    checkedAt: null,
    reason: '授权状态尚未确认。',
  };
}

function unconfirmedIdentities(): AdapterIdentityState {
  return { state: 'unconfirmed', count: null };
}

function adapterRow(value: unknown): {
  name: string;
  provider: string;
  status: string;
  configured: boolean;
  desired: boolean;
  processRunning: boolean;
  online: boolean;
  error?: string;
  agent: string | null;
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('平台接入状态格式不正确。');
  }
  const row = value as Record<string, unknown>;
  if (typeof row.name !== 'string' || !row.name
    || typeof row.provider !== 'string' || !row.provider
    || typeof row.status !== 'string' || !row.status
    || typeof row.configured !== 'boolean'
    || typeof row.desired !== 'boolean'
    || typeof row.processRunning !== 'boolean'
    || typeof row.online !== 'boolean'
    || (row.agent !== undefined && row.agent !== null && typeof row.agent !== 'string')) {
    throw new Error('平台接入状态格式不正确。');
  }
  return {
    name: row.name,
    provider: row.provider,
    status: row.status,
    configured: row.configured,
    desired: row.desired,
    processRunning: row.processRunning,
    online: row.online,
    error: typeof row.error === 'string' ? row.error : undefined,
    agent: typeof row.agent === 'string' ? row.agent : null,
  };
}

export function createPlatformIntegrationsApiFromClient(
  client: Pick<GeneratedClient, 'adapter'> & Partial<Pick<GeneratedClient, 'management'>>,
  access?: MethodAccess,
): PlatformIntegrationsApi {
  const remove = client.management?.adapter?.remove;
  return {
    ...(typeof remove === 'function' && typeof remove.preview === 'function' ? {
      async removePreview(name: string): Promise<RemovePreview> {
        assertMethodAvailable(access, 'management.adapter.remove.preview', { name });
        const result = await remove.preview({ name });
        if (result.name !== name || !result.operationId || result.ok !== true) throw new Error('删除预览尚未确认。');
        return { ...result, online: false };
      },
      async remove(name: string, expected: RemovePreview): Promise<void> {
        if (expected.name !== name || !expected.operationId) throw new Error('请重新预览后确认删除。');
        const params = { operationId: expected.operationId };
        assertMethodAvailable(access, 'management.adapter.remove', params);
        const result = await remove(params);
        if (result.name !== name || result.ok !== true || result.removed !== true) throw new Error('删除结果尚未确认。');
      },
    } : {}),
    capabilities: {
      get add() {
        return accessRefusal(access, 'adapter.register') === null
          ? { available: true } : capabilityOf(access, 'adapter.onboard.begin');
      },
      get register() { return capabilityOf(access, 'adapter.register'); },
      get onboard() { return capabilityOf(access, 'adapter.onboard.begin'); },
      get authorization() { return capabilityOf(access, 'adapter.authorization.begin'); },
      get identities() { return capabilityOf(access, 'adapter.identities.sync'); },
    },
    async register(input) {
      const params = { provider: 'lark' as const, name: input.name, appId: input.appId, appSecret: input.secret };
      assertMethodAvailable(access, 'adapter.register', params);
      const response = await client.adapter.register(params);
      if (!response || response.ok !== true || response.configured !== true
        || response.adapter?.provider !== 'lark' || response.adapter.name !== input.name
        || response.adapter.appId !== input.appId || typeof response.reloaded !== 'boolean'
        || response.nextAction !== (response.reloaded ? 'none' : 'reload')) {
        throw new Error('平台接入添加结果未知。');
      }
      return response;
    },
    async beginAdd(input) {
      if (input.mode !== 'create') throw new TypeError('onboarding takes the create mode only');
      const params = { provider: 'lark' as const, name: input.name, operationKey: crypto.randomUUID() };
      assertMethodAvailable(access, 'adapter.onboard.begin', params);
      return client.adapter.onboard.begin(params);
    },
    async currentAdd(name) {
      assertMethodAvailable(access, 'adapter.onboard.operation', { name });
      try { return await client.adapter.onboard.operation({ name }); }
      catch (error) {
        if (error instanceof ClientError && error.code === 'ADAPTER_OPERATION_NOT_FOUND') return null;
        throw error;
      }
    },
    async addOperation(operationId) {
      assertMethodAvailable(access, 'adapter.onboard.operation', { operationId });
      return client.adapter.onboard.operation({ operationId });
    },
    async cancelAdd(operationId) {
      assertMethodAvailable(access, 'adapter.onboard.cancel', { operationId });
      return client.adapter.onboard.cancel({ operationId });
    },
    async list() {
      assertMethodAvailable(access, 'adapter.list', {});
      const response = await client.adapter.list({});
      if (!response || response.ok !== true || !Array.isArray(response.adapters)) {
        throw new Error('平台接入列表尚未确认。');
      }
      const rows = response.adapters.map(adapterRow);
      return rows.map(row => ({
        ...row,
        boundAgent: row.agent,
        authorization: unconfirmedAuthorization(row.name),
        identities: unconfirmedIdentities(),
      }));
    },
    async start(name) {
      assertMethodAvailable(access, 'adapter.start', { name });
      const response = await client.adapter.start({ name });
      if (!response || response.ok !== true || !response.adapter || response.adapter.name !== name) {
        throw new Error('启动结果尚未确认。');
      }
    },
    async listIdentities(name) {
      assertMethodAvailable(access, 'adapter.identities.list', { name });
      const response = await client.adapter.identities.list({ name });
      if (!response || response.ok !== true || response.adapter !== name
        || !Array.isArray(response.identities) || response.count !== response.identities.length) {
        throw new Error('平台身份读取失败。');
      }
      return response;
    },
    async authorizationStatus(name) {
      assertMethodAvailable(access, 'adapter.authorization.status', { name });
      const result = await client.adapter.authorization.status({ name });
      if (!result || result.ok !== true || result.adapter !== name) throw new Error('权限读取失败。');
      return result;
    },
    async beginAuthorization(name) {
      const params = { name, operationKey: crypto.randomUUID(), capabilities: ['message-send' as const, 'receive-p2p' as const, 'receive-group-mention' as const] };
      assertMethodAvailable(access, 'adapter.authorization.begin', params);
      return client.adapter.authorization.begin(params);
    },
    async currentAuthorization(name) {
      assertMethodAvailable(access, 'adapter.authorization.operation', { name });
      try {
        return await client.adapter.authorization.operation({ name });
      } catch (error) {
        if (error instanceof ClientError && error.code === 'ADAPTER_OPERATION_NOT_FOUND') return null;
        throw error;
      }
    },
    async authorizationOperation(operationId) {
      assertMethodAvailable(access, 'adapter.authorization.operation', { operationId });
      return client.adapter.authorization.operation({ operationId });
    },
    async cancelAuthorization(operationId) {
      assertMethodAvailable(access, 'adapter.authorization.cancel', { operationId });
      return client.adapter.authorization.cancel({ operationId });
    },
    async syncIdentities(name) {
      assertMethodAvailable(access, 'adapter.identities.sync', { name });
      const response = await client.adapter.identities.sync({ name });
      if (!response || response.adapter !== name || typeof response.ok !== 'boolean'
        || !Array.isArray(response.errors) || !Array.isArray(response.chats)) {
        throw new Error('平台身份同步结果未知，请读取平台身份。');
      }
      return response;
    },
    async stop(name) {
      assertMethodAvailable(access, 'adapter.stop', { name });
      const response = await client.adapter.stop({ name });
      if (!response || response.ok !== true || !response.adapter || response.adapter.name !== name) {
        throw new Error('停止结果尚未确认。');
      }
    },
    async reload() {
      assertMethodAvailable(access, 'adapter.reload', {});
      const response = await client.adapter.reload({});
      if (!response || response.ok !== true) {
        throw new Error('重新装载结果尚未确认。');
      }
    },
  };
}
