import { describe, expect, it, vi } from 'vitest';
import { createBackendConnection, type BackendDriver } from '../src/app/backend';
import { createConnectedGuiCapabilities, createFirstRunConfigureAgent } from '../src/app/capabilities';
import { MethodRefusedError, TransportError } from '../src/client';
import type { AgentDraft, AgentMutationGateway } from '../src/features/agents';
import { CREATE_NEW_AGENT } from './fixtures/agents/agent-mutations';

const DESCRIPTOR = {
  interfaceVersion: 1,
  driver: 'contract-mock',
  phase: 'ready',
  generation: 'generation-1',
  catalogDigest: 'sha256:catalog',
  methods: [
    { name: 'agent.create', callable: true, state: 'available', mutates: true },
    { name: 'agent.start', callable: true, state: 'available', mutates: true },
    { name: 'agent.secret.grant', callable: true, state: 'available', mutates: true },
    { name: 'agent.secret.provider-list', callable: true, state: 'available', mutates: false },
    { name: 'agent.provider.login', callable: true, state: 'available', mutates: true },
  ],
} as const;

it('keeps explicit first-run creation separate from user-approved start', async () => {
  const start = vi.fn();
  const gateway = { create: vi.fn(async () => ({ kind: 'created', created: true, actorUri: 'agent:test:node:worker', agent: { id: 'worker' } })) };
  const configure = createFirstRunConfigureAgent(gateway as never, undefined, undefined, undefined, start);
  const result = await configure(draft({ credential: undefined, provider: undefined, modelConfiguration: { operationId: 'model-first-run', configuration: {
    authKind: 'connection', harness: 'pi', mode: 'headless', model: 'hy4-flash', connectionRef: '0123456789abcdef0123456789abcdef', configRevision: 1,
  } } }));
  expect(result).toMatchObject({ status: 'incomplete', created: true, actorUri: 'agent:test:node:worker' });
  expect(start).not.toHaveBeenCalled();
});

function draft(overrides: Partial<AgentDraft> = {}): AgentDraft {
  return {
    displayName: 'squire',
    role: '助手',
    modelServiceId: '',
    organizationId: null,
    provider: 'deepseek',
    model: 'deepseek-flash',
    preferredHarness: 'pi',
    credential: {
      kind: 'key',
      keyId: 'deepseek-official:key',
      provider: 'deepseek',
    },
    ...overrides,
  };
}

function composition() {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const connection = createBackendConnection({
    async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
      const record = params as Record<string, unknown>;
      calls.push({ method, params: record });
      switch (method) {
        case 'agent.create':
          return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
        case 'agent.secret.grant':
          return {
            ok: true,
            grant: {
              actor: record.actor,
              grantId: record.grantId,
              entryId: record.entryId,
              fieldName: record.fieldName,
              revision: record.revision,
              source: 'user-provider',
              environmentNames: record.environmentNames,
            },
          } as TResult;
        case 'agent.provider.login':
          return { actor: record.actor, provider: record.provider } as TResult;
        case 'agent.start':
          return { accepted: true, running: true, warnings: [] } as TResult;
        default:
          throw new Error(`unexpected method ${method}`);
      }
    },
  }, DESCRIPTOR);
  return { calls, capabilities: createConnectedGuiCapabilities(connection) };
}

describe('formal first-run gate', () => {
  const LOOP = [
    'onboarding.status',
    'onboarding.initialize',
    'onboarding.firstRun.begin',
    'onboarding.firstRun.bind',
    'onboarding.firstRun.confirm',
    'onboarding.firstRun.cancel',
    'agent.create',
    'agent.start',
  ];
  const gate = (missing: readonly string[] = [], driver: BackendDriver = 'daemon') => {
    const methods = LOOP.filter((name) => !missing.includes(name))
      .map((name) => ({ name, callable: true, state: 'available' as const, mutates: false }));
    const connection = createBackendConnection({
      async invoke<TResult>(): Promise<TResult> {
        throw new Error('the gate only reads the catalog');
      },
    }, { ...DESCRIPTOR, driver, methods });
    return createConnectedGuiCapabilities(connection).firstRunWizardEnabled;
  };

  it('opens only for a daemon that serves the whole durable loop', () => {
    expect(gate()).toBe(true);
    for (const missing of [
      'onboarding.initialize',
      'agent.create',
      'agent.start',
      'onboarding.firstRun.begin',
      'onboarding.firstRun.bind',
      'onboarding.firstRun.confirm',
      'onboarding.firstRun.cancel',
    ]) {
      expect(gate([missing]), missing).toBe(false);
    }
    expect(gate([], 'contract-mock')).toBe(false);
  });

  it.each(['bootstrap', 'ready'] as const)('offers the journey across the %s phase without authorizing later writes', async (phase) => {
    const invoke = vi.fn(async (): Promise<never> => { throw new Error('must not dispatch'); });
    const methods = LOOP.map(name => {
      const blocked = phase === 'bootstrap' ? !['onboarding.status', 'onboarding.initialize'].includes(name)
        : name === 'onboarding.initialize';
      return { name, callable: !blocked, state: blocked ? 'not-ready' : 'available', mutates: name !== 'onboarding.status' };
    });
    const connection = createBackendConnection({ invoke }, { ...DESCRIPTOR, driver: 'daemon', phase, methods });
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.firstRunWizardEnabled).toBe(true);
    expect(capabilities.firstRun.initializeDevice).toBeTypeOf('function');
    expect(capabilities.firstRun.replyConfirmation).toBeDefined();
    expect('prepareSquire' in capabilities.firstRun).toBe(false);
    expect('cancelPreparation' in capabilities.firstRun).toBe(false);
    expect('getSetupState' in capabilities.firstRun).toBe(false);
    if (phase === 'bootstrap') {
      await expect(capabilities.firstRun.configureAgent?.(draft())).resolves.toMatchObject({ status: 'unavailable' });
    } else {
      await expect(capabilities.firstRun.initializeDevice?.()).resolves.toMatchObject({ status: 'unavailable' });
    }
    expect(invoke).not.toHaveBeenCalled();
  });

  it.each(['disabled', 'unsupported', 'unknown'])('keeps the journey closed for a %s method', (state) => {
    const methods = LOOP.map(name => ({ name, callable: name !== 'agent.start', state: name === 'agent.start' ? state : 'available', mutates: false }));
    const connection = createBackendConnection({ invoke: vi.fn() }, { ...DESCRIPTOR, driver: 'daemon', methods });
    expect(createConnectedGuiCapabilities(connection).firstRunWizardEnabled).toBe(false);
  });

  it('exposes the durable skip action when the daemon declares it', async () => {
    const invoke = vi.fn(async (method: string, _params: object = {}) => {
      if (method !== 'onboarding.firstRun.skip') throw new Error(`unexpected method ${method}`);
      return {
        ok: true,
        firstRun: {
          state: 'recorded', revision: 1, confirmed: [],
          updatedAt: '2026-11-19T00:00:00Z', skipped: true,
        },
      };
    });
    const methods = [...LOOP, 'onboarding.firstRun.skip']
      .map(name => ({ name, callable: true, state: 'available' as const, mutates: true }));
    const connection = createBackendConnection({
      invoke: invoke as unknown as <TResult>(method: string, params?: object) => Promise<TResult>,
    }, { ...DESCRIPTOR, driver: 'daemon', methods });
    const capabilities = createConnectedGuiCapabilities(connection);
    await expect(capabilities.firstRun.skipFirstRun?.(0)).resolves.toMatchObject({
      status: 'skipped', firstRun: { skipped: true },
    });
    expect(invoke).toHaveBeenCalledWith(
      'onboarding.firstRun.skip',
      expect.objectContaining({ expectedRevision: 0 }),
      expect.objectContaining({ retry: 'never' }),
    );
  });
});

describe('connected first-run runtime configuration', () => {
  it('creates the Agent and grants the selected Key through the shared API', async () => {
    const { calls, capabilities } = composition();

    await expect(capabilities.firstRun.configureAgent?.(draft())).resolves.toMatchObject({
      status: 'configured',
      created: true,
    });

    expect(calls.map((call) => call.method)).toEqual([
      'agent.create',
      'agent.secret.grant',
      'agent.start',
    ]);
    expect(calls[0].params).toMatchObject({
      name: 'squire',
      provider: 'deepseek',
      model: 'deepseek-flash',
      preferredHarness: 'pi',
    });
    expect(calls[1].params).toMatchObject({
      actor: expect.stringMatching(/^agent:/),
      source: 'user-provider',
      grantId: 'model-deepseek',
      entryId: 'deepseek-official',
      fieldName: 'key',
      environmentNames: ['DEEPSEEK_API_KEY'],
      revision: 1,
    });
    expect(calls[2].params).toMatchObject({
      name: expect.any(String),
      harness: 'pi',
      provider: 'deepseek',
      model: 'deepseek-flash',
      cwd: '/work/worker',
      approve: true,
    });
  });

  it('refuses to approve a Pi start without the actual created workspace', async () => {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        calls.push(method);
        const record = params as Record<string, unknown>;
        if (method === 'agent.create') {
          const created = CREATE_NEW_AGENT.result as Record<string, unknown>;
          const { cwd: _cwd, ...withoutCwd } = created.agent as Record<string, unknown>;
          return {
            ...created, created: true, agent: withoutCwd,
          } as TResult;
        }
        if (method === 'agent.secret.grant') {
          return {
            ok: true,
            grant: {
              actor: record.actor, grantId: record.grantId, entryId: record.entryId,
              fieldName: record.fieldName, revision: record.revision,
              source: 'user-provider', environmentNames: record.environmentNames,
            },
          } as TResult;
        }
        throw new Error(`unexpected method ${method}`);
      },
    }, DESCRIPTOR);
    const result = await createConnectedGuiCapabilities(connection).firstRun.configureAgent?.(draft());
    expect(result).toMatchObject({
      status: 'unknown',
      created: true,
      actorUri: expect.stringMatching(/^agent:/),
    });
    expect(calls).toEqual(['agent.create', 'agent.secret.grant']);
  });

  it('writes native Auth files through the shared Agent API', async () => {
    const { calls, capabilities } = composition();

    await expect(capabilities.firstRun.configureAgent?.(draft({
      provider: null,
      model: null,
      credential: {
        kind: 'auth',
        harness: 'pi',
        files: [{ filename: 'auth.json', contentBase64: 'ZmFrZQ==' }],
      },
    }))).resolves.toMatchObject({ status: 'configured', created: true });

    expect(calls.map((call) => call.method)).toEqual([
      'agent.create',
      'agent.provider.login',
      'agent.start',
    ]);
    expect(calls[1].params).toMatchObject({
      provider: 'pi',
      files: [{ filename: 'auth.json' }],
    });
  });

  it('does not silently take over an existing Agent name', async () => {    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string): Promise<TResult> {
        calls.push(method);
        return { ...CREATE_NEW_AGENT.result, created: false } as TResult;
      },
    }, DESCRIPTOR);
    const capabilities = createConnectedGuiCapabilities(connection);

    await expect(capabilities.firstRun.configureAgent?.(draft())).resolves.toMatchObject({
      status: 'failed',
      message: expect.stringContaining('同名 Agent 已存在'),
    });
    expect(calls).toEqual(['agent.create']);
  });

  it.each(['key', 'auth'] as const)(
    'keeps an unknown %s credential write unknown after create',
    async (kind) => {
      const calls: string[] = [];
      const connection = createBackendConnection({
        async invoke<TResult>(method: string): Promise<TResult> {
          calls.push(method);
          if (method === 'agent.create') {
            return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
          }
          throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'review timeout', {
            method,
            requestId: 'review',
            status: null,
            dispatch: 'unknown',
            catalogDigest: null,
            attempts: 1,
            startedAt: 0,
            durationMs: 1,
          });
        },
      }, DESCRIPTOR);
      const capabilities = createConnectedGuiCapabilities(connection);

      const result = await capabilities.firstRun.configureAgent?.(draft(
        kind === 'key'
          ? {}
          : {
              provider: null,
              model: null,
              credential: {
                kind: 'auth',
                harness: 'pi',
                files: [{ filename: 'auth.json', contentBase64: 'ZmFrZQ==' }],
              },
            },
      ));

      expect(result).toMatchObject({
        status: 'unknown',
        created: true,
        actorUri: expect.stringMatching(/^agent:/),
        stage: kind === 'key' ? 'credential-key' : 'credential-auth',
      });
      expect(calls).toEqual([
        'agent.create',
        kind === 'key' ? 'agent.secret.grant' : 'agent.provider.login',
      ]);
    },
  );

  it.each(['key', 'auth'] as const)(
    'refuses before create when the %s write port is absent',
    async (kind) => {
      const create: AgentMutationGateway['create'] = vi.fn(async () => {
        throw new Error('create must not run without its write port');
      });
      const configure = createFirstRunConfigureAgent({ create }, undefined, undefined);

      const result = await configure(draft(
        kind === 'key'
          ? {}
          : {
              provider: null,
              model: null,
              credential: {
                kind: 'auth',
                harness: 'pi',
                files: [{ filename: 'auth.json', contentBase64: 'ZmFrZQ==' }],
              },
            },
      ));

      expect(result).toMatchObject({
        status: 'unavailable',
        refusal: { code: 'GUI_CAPABILITY_UNAVAILABLE' },
      });
      expect(create).not.toHaveBeenCalled();
    },
  );

  it.each(['key', 'auth'] as const)(
    'refuses before create when the catalog refuses the %s write',
    async (kind) => {
      const calls: string[] = [];
      const method = kind === 'key' ? 'agent.secret.grant' : 'agent.provider.login';
      const connection = createBackendConnection({
        async invoke<TResult>(called: string): Promise<TResult> {
          calls.push(called);
          return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
        },
      }, {
        ...DESCRIPTOR,
        methods: DESCRIPTOR.methods.map((entry) => entry.name === method
          ? { ...entry, callable: false }
          : entry),
      });
      const capabilities = createConnectedGuiCapabilities(connection);

      const result = await capabilities.firstRun.configureAgent?.(draft(
        kind === 'key'
          ? {}
          : {
              provider: null,
              model: null,
              credential: {
                kind: 'auth',
                harness: 'pi',
                files: [{ filename: 'auth.json', contentBase64: 'ZmFrZQ==' }],
              },
            },
      ));

      expect(result).toMatchObject({
        status: 'unavailable',
        refusal: { code: 'METHOD_NOT_CALLABLE', source: method },
      });
      expect(calls).toEqual([]);
    },
  );

  it('keeps a refused credential write unavailable instead of unknown', async () => {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string): Promise<TResult> {
        calls.push(method);
        if (method === 'agent.create') {
          return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
        }
        throw new MethodRefusedError(method, 'METHOD_NOT_AVAILABLE');
      },
    }, DESCRIPTOR);
    const capabilities = createConnectedGuiCapabilities(connection);

    await expect(capabilities.firstRun.configureAgent?.(draft())).resolves.toMatchObject({
      status: 'unavailable',
      refusal: { code: 'METHOD_NOT_AVAILABLE' },
      created: true,
      actorUri: expect.stringMatching(/^agent:/),
      stage: 'credential-key',
    });
    expect(calls).toEqual(['agent.create', 'agent.secret.grant']);
  });

  it('keeps an unconfirmed or unknown Agent start unknown', async () => {
    for (const outcome of ['not-running', 'unknown'] as const) {
      const calls: string[] = [];
      const connection = createBackendConnection({
        async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
          calls.push(method);
          const record = params as Record<string, unknown>;
          if (method === 'agent.create') {
            return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
          }
          if (method === 'agent.secret.grant') {
            return {
              ok: true,
              grant: {
                actor: record.actor, grantId: record.grantId, entryId: record.entryId,
                fieldName: record.fieldName, revision: record.revision,
                source: 'user-provider', environmentNames: record.environmentNames,
              },
            } as TResult;
          }
          if (method === 'agent.start' && outcome === 'not-running') {
            return { accepted: true, running: false, warnings: [] } as TResult;
          }
          throw new TransportError('timeout', 'REQUEST_TIMEOUT', 'review timeout', {
            method, requestId: 'review', status: null, dispatch: 'unknown',
            catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 1,
          });
        },
      }, DESCRIPTOR);
      const result = await createConnectedGuiCapabilities(connection).firstRun.configureAgent?.(draft());
      expect(result).toMatchObject({
        status: 'unknown',
        created: true,
        actorUri: expect.stringMatching(/^agent:/),
      });
      expect(calls).toEqual(['agent.create', 'agent.secret.grant', 'agent.start']);
    }
  });

  it('reports a refused Agent start without losing the created Agent', async () => {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        calls.push(method);
        const record = params as Record<string, unknown>;
        if (method === 'agent.create') {
          return { ...CREATE_NEW_AGENT.result, created: true } as TResult;
        }
        if (method === 'agent.secret.grant') {
          return {
            ok: true,
            grant: {
              actor: record.actor, grantId: record.grantId, entryId: record.entryId,
              fieldName: record.fieldName, revision: record.revision,
              source: 'user-provider', environmentNames: record.environmentNames,
            },
          } as TResult;
        }
        throw new MethodRefusedError('agent.start', 'METHOD_NOT_AVAILABLE');
      },
    }, DESCRIPTOR);
    const result = await createConnectedGuiCapabilities(connection).firstRun.configureAgent?.(draft());
    expect(result).toMatchObject({
      status: 'unavailable',
      refusal: { code: 'METHOD_NOT_AVAILABLE' },
      created: true,
      actorUri: expect.stringMatching(/^agent:/),
    });
    expect(calls).toEqual(['agent.create', 'agent.secret.grant', 'agent.start']);
  });
});
