import { DomainError } from '../../client';
import type { AdapterAuthorization, AdapterIdentityRow, AdapterOperation, AdapterOperationStatus, AddPlatformInput, PlatformIntegration, PlatformIntegrationsApi } from '../../features/connections';

let mockSequence = 0;

function mockOperation(
  kind: AdapterOperation['kind'],
  adapter: string,
  status: AdapterOperationStatus,
  extra: Partial<AdapterOperation> = {},
): AdapterOperation {
  mockSequence += 1;
  return {
    operationId: `mock-op-${mockSequence}`,
    adapter,
    provider: 'lark',
    kind,
    status,
    ...extra,
  };
}

export interface MockPlatformIntegrationsSeed {
  readonly integrations?: readonly PlatformIntegration[];
  readonly identities?: Readonly<Record<string, readonly AdapterIdentityRow[]>>;
}

export function createMockPlatformIntegrationsApi(
  seed: MockPlatformIntegrationsSeed = {},
): PlatformIntegrationsApi {
  let integrations: PlatformIntegration[] = [...(seed.integrations ?? [{
    name: 'feishu-main',
    provider: 'lark',
    status: 'stopped',
    configured: true,
    desired: false,
    processRunning: false,
    online: false,
    boundAgent: null,
    authorization: {
      adapter: 'feishu-main',
      appId: 'cli_mock',
      state: 'unauthorized',
      capabilities: [],
      missingCapabilities: ['message-send', 'receive-p2p', 'receive-group-mention'],
      checkedAt: Date.now(),
    },
    identities: { state: 'not-synced', count: 0 },
  }])];
  interface MockOperation extends AdapterOperation {
    readonly input?: AddPlatformInput;
  }
  const operations = new Map<string, MockOperation>();
  let identityRows: Record<string, readonly AdapterIdentityRow[]> = { ...(seed.identities ?? {}) };
  integrations = integrations.map(row => {
    const known = identityRows[row.name];
    return known && known.length > 0 && row.identities.state === 'not-synced'
      ? { ...row, identities: { state: 'synced' as const, count: known.length } }
      : row;
  });

  function replace(name: string, change: (row: PlatformIntegration) => PlatformIntegration): void {
    integrations = integrations.map(row => row.name === name ? change(row) : row);
  }

  function find(name: string): PlatformIntegration {
    const row = integrations.find(value => value.name === name);
    if (!row) throw new Error('平台接入不存在。');
    return row;
  }

  function authorized(row: PlatformIntegration, appId: string | null): AdapterAuthorization {
    return {
      ...row.authorization,
      appId,
      state: 'authorized',
      capabilities: ['message-send', 'receive-p2p', 'receive-group-mention'],
      missingCapabilities: [],
      checkedAt: Date.now(),
      reason: undefined,
    };
  }

  function addIntegration(add: AddPlatformInput): void {
    integrations = [...integrations, {
      name: add.name,
      provider: add.provider,
      status: 'stopped',
      configured: true,
      desired: false,
      processRunning: false,
      online: false,
      boundAgent: null,
      authorization: {
        adapter: add.name,
        appId: add.mode === 'adopt' ? add.appId ?? 'cli_mock' : 'cli_mock_new',
        state: 'unauthorized',
        capabilities: [],
        missingCapabilities: ['message-send', 'receive-p2p', 'receive-group-mention'],
        checkedAt: Date.now(),
      },
      identities: { state: 'not-synced', count: 0 },
    }];
    identityRows[add.name] = [];
  }

  function checkAdd(input: AddPlatformInput): void {
    if (integrations.some(row => row.name === input.name)
      || [...operations.values()].some(operation => operation.adapter === input.name
        && ['starting', 'awaiting_user', 'checking', 'unknown'].includes(operation.status))) {
      throw new Error('同名平台接入已存在。');
    }
  }

  function finishAdd(operation: MockOperation): AdapterOperation {
    const add = operation.input;
    if (add) addIntegration(add);
    return { ...operation, status: 'succeeded', adapter: add?.name ?? operation.adapter };
  }

  return {
    capabilities: {
      add: { available: true },
      register: { available: true },
      onboard: { available: true },
      authorization: { available: true },
      identities: { available: true },
    },
    async list() {
      return integrations.map(row => ({ ...row }));
    },
    async start(name) {
      replace(name, row => ({ ...row, desired: true, processRunning: true, online: true, status: 'online' }));
    },
    async stop(name) {
      replace(name, row => ({ ...row, desired: false, processRunning: false, online: false, status: 'stopped' }));
    },
    async reload() {},
    async authorizationStatus(name) {
      const row = find(name);
      return { ...row.authorization };
    },
    async register(input) {
      const add: AddPlatformInput = { provider: input.provider, name: input.name, appId: input.appId, mode: 'adopt' };
      try {
        checkAdd(add);
      } catch {
        throw new DomainError('ADAPTER_EXISTS', '添加被拒绝。', null, {
          method: 'adapter.register', requestId: 'fixture-register', status: 409, dispatch: 'dispatched',
          catalogDigest: 'fixture', attempts: 1, startedAt: 0, durationMs: 0,
        });
      }
      if (!input.appId.trim() || !input.secret) throw new Error('请输入 App ID 和 App Secret。');
      addIntegration(add);
      return { ok: true, adapter: { provider: 'lark', name: input.name, appId: input.appId },
        configured: true, reloaded: true, nextAction: 'none' };
    },
    async beginAdd(input) {
      checkAdd(input);
      const operation: MockOperation = {
        ...mockOperation('add', input.name, input.mode === 'adopt' ? 'succeeded' : 'awaiting_user', {
          simulated: true,
        }),
        input: { provider: input.provider, name: input.name, mode: input.mode, appId: input.appId },
      };
      if (input.mode === 'adopt') {
        const done = finishAdd(operation);
        operations.set(operation.operationId, done);
        return done;
      }
      operations.set(operation.operationId, operation);
      return operation;
    },
    async addOperation(operationId) {
      const operation = operations.get(operationId);
      if (!operation) throw new Error('操作不存在。');
      if (operation.status === 'succeeded') return operation;
      if (operation.status === 'awaiting_user' || operation.status === 'checking') {
        const done = finishAdd(operation);
        operations.set(operationId, done);
        return done;
      }
      return operation;
    },
    async cancelAdd(operationId) {
      const operation = operations.get(operationId);
      if (!operation) throw new Error('操作不存在。');
      if (!['starting', 'awaiting_user', 'checking', 'unknown'].includes(operation.status)) return operation;
      const cancelled = { ...operation, status: 'cancelled' as const };
      operations.set(operationId, cancelled);
      return cancelled;
    },
    async beginAuthorization(name) {
      const row = find(name);
      if (row.authorization.state === 'authorized') {
        return mockOperation('authorization', name, 'succeeded', { authorization: row.authorization });
      }
      const operation = mockOperation('authorization', name, 'awaiting_user', {
        simulated: true,
      });
      operations.set(operation.operationId, operation);
      return operation;
    },
    async authorizationOperation(operationId) {
      const operation = operations.get(operationId);
      if (!operation) throw new Error('操作不存在。');
      if (operation.status === 'awaiting_user' || operation.status === 'checking') {
        const row = find(operation.adapter);
        const done: AdapterOperation = {
          ...operation,
          status: 'succeeded',
          authorization: authorized(row, row.authorization.appId),
        };
        replace(operation.adapter, current => ({ ...current, authorization: done.authorization! }));
        operations.set(operationId, done);
        return done;
      }
      return operation;
    },
    async cancelAuthorization(operationId) {
      const operation = operations.get(operationId);
      if (!operation) throw new Error('操作不存在。');
      if (!['starting', 'awaiting_user', 'checking', 'unknown'].includes(operation.status)) return operation;
      const cancelled = { ...operation, status: 'cancelled' as const };
      operations.set(operationId, cancelled);
      return cancelled;
    },
    async syncIdentities(name) {
      const row = find(name);
      const appId = row.authorization.appId ?? `app-${name}`;
      const now = Date.now();
      const observed: AdapterIdentityRow[] = [
        {
          kind: 'app',
          platformId: appId,
          displayName: name,
          unionId: null,
          standing: 'observed',
          source: `bot-info:${appId}`,
          firstSeenMs: now,
          lastSeenMs: now,
        },
        {
          kind: 'bot',
          platformId: 'ou_mock_bot',
          displayName: name,
          unionId: null,
          standing: 'observed',
          source: `bot-info:${appId}`,
          firstSeenMs: now,
          lastSeenMs: now,
        },
        {
          kind: 'user',
          platformId: 'ou_mock_user',
          displayName: '张三',
          unionId: null,
          standing: 'observed',
          source: 'chat-members:oc_mock_chat',
          firstSeenMs: now,
          lastSeenMs: now,
        },
      ];
      const stored = new Map((identityRows[name] ?? []).map(item => [`${item.kind}:${item.platformId}`, item]));
      for (const item of observed) {
        const key = `${item.kind}:${item.platformId}`;
        const previous = stored.get(key);
        stored.set(key, previous ? {
          ...previous,
          displayName: item.displayName ?? previous.displayName,
          unionId: item.unionId ?? previous.unionId,
          source: previous.standing === 'verified' ? previous.source : item.source,
          firstSeenMs: previous.firstSeenMs ?? item.firstSeenMs,
          lastSeenMs: item.lastSeenMs,
        } : item);
      }
      const rows = [...stored.values()];
      identityRows[name] = rows;
      replace(name, current => ({ ...current, identities: { state: 'synced', count: rows.length } }));
      return {
        adapter: name,
        ok: true,
        appId,
        bot: { openId: 'ou_mock_bot', appName: name },
        chats: [{
          chatId: 'oc_mock_chat',
          name: '示例群',
          memberTotal: 1,
          membersRecorded: 1,
          complete: true,
        }],
        chatsComplete: true,
        identitiesRecorded: observed.length,
        errors: [],
      };
    },
    async listIdentities(name) {
      find(name);
      const identities = (identityRows[name] ?? []).map(row => ({ ...row }));
      return { adapter: name, count: identities.length, identities };
    },
    async removePreview(name) {
      const row = find(name);
      return {
        name: row.name,
        provider: row.provider,
        boundAgent: row.boundAgent,
        online: row.online,
        processRunning: row.processRunning,
      };
    },
    async remove(name, expected) {
      if (!integrations.some(row => row.name === name)) throw new Error('平台接入不存在。');
      if (expected.processRunning) throw new Error('请先停止平台接入。');
      integrations = integrations.filter(row => row.name !== name);
      delete identityRows[name];
    },
  };
}
