import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../../src/App';
import { createBackendConnection } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { createEmptyState } from '../../src/state/appReducer';
import { HttpError } from '../../src/client';

const DESCRIPTOR = {
  interfaceVersion: 1,
  driver: 'contract-mock',
  phase: 'ready',
  generation: 'generation-1',
  catalogDigest: 'sha256:catalog',
} as const;

function resultFor(method: string, params: Record<string, unknown>): unknown {
  switch (method) {
    case 'onboarding.status': return { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true, owner: 'test@example.invalid', deviceReady: true, initialized: true };
    case 'org.list': return { orgs: [], leftOrgs: [] };
    case 'message.query': return { ok: true, view: params.view, actor: 'user:owner', entries: [] };
    case 'conversations.history': return { ok: true, conversationId: params.conversationId, entries: [], nextCursor: null,
      threadId: null, legacyConversationId: null };
    case 'targets': return { targets: [{ targetUri: 'agent:owner:node:worker', targetKind: 'agent',
      deliverable: true, actor: 'agent:owner:node:worker', status: 'online' }] };
    case 'hosts': return { hosts: [] };
    case 'top.snapshot': return { daemon: {}, quota: {}, actors: [], stranded: [] };
    case 'agent.list': return { agents: [] };
    case 'agent.start': return { accepted: true, running: true, warnings: [] };
    case 'agent.provider.default': return { provider: null };
    case 'agent.provider.login': return { actor: params.actor, provider: params.provider };
    case 'agent.secret.list': return { grants: [] };
    case 'agent.secret.provider-write': return {
      ok: true, entryId: params.entryId, fieldNames: [params.fieldName],
    };
    case 'adapter.list': return { ok: true, adapters: [] };
    case 'adapter.start': return {
      ok: true,
      changed: true,
      adapter: {
        id: `lark-${params.name}`, provider: 'lark', name: params.name, status: 'running',
        online: true, configured: true, desired: true, processRunning: true,
      },
    };
    default: throw new Error(`unexpected method ${method}`);
  }
}

function composition() {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const connection = createBackendConnection({
    async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
      const record = params as Record<string, unknown>;
      calls.push({ method, params: record });
      return resultFor(method, record) as TResult;
    },
  }, DESCRIPTOR);
  return { connection, calls, capabilities: createConnectedGuiCapabilities(connection) };
}

function callableComposition() {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const connection = createBackendConnection({
    async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
      const record = params as Record<string, unknown>;
      calls.push({ method, params: record });
      return resultFor(method, record) as TResult;
    },
  }, {
    ...DESCRIPTOR,
    methods: [
      { name: 'onboarding.status', callable: true, state: 'available', mutates: false },
      { name: 'agent.list', callable: true, state: 'available', mutates: false },
      { name: 'agent.create', callable: true, state: 'available', mutates: true },
      { name: 'agent.start', callable: true, state: 'available', mutates: true },
      { name: 'agent.provider.login', callable: true, state: 'available', mutates: true },
      { name: 'agent.provider.default', callable: true, state: 'available', mutates: true },
      { name: 'agent.secret.list', callable: true, state: 'available', mutates: false },
      { name: 'agent.secret.provider-write', callable: true, state: 'available', mutates: true },
      { name: 'adapter.list', callable: true, state: 'available', mutates: false },
      { name: 'adapter.start', callable: true, state: 'available', mutates: true },
      { name: 'adapter.stop', callable: true, state: 'available', mutates: true },
      { name: 'adapter.reload', callable: true, state: 'available', mutates: true },
      { name: 'message.query', callable: true, state: 'available', mutates: false },
      { name: 'org.create', callable: true, state: 'available', mutates: true },
      { name: 'org.fetch', callable: true, state: 'available', mutates: true },
    ],
  });
  return { connection, calls, capabilities: createConnectedGuiCapabilities(connection) };
}

describe('ordinary App composition', () => {
  it('injects the frozen credential mapping through the same backend connection', async () => {
    const { capabilities, calls } = callableComposition();

    await expect(capabilities.firstRun.saveModelKey?.('synthetic-key')).resolves.toEqual({ status: 'saved' });

    expect(calls.filter(call => call.method === 'agent.secret.list')).toHaveLength(1);
    expect(calls.filter(call => call.method === 'agent.secret.provider-write')).toEqual([{
      method: 'agent.secret.provider-write',
      params: { entryId: 'deepseek-official', fieldName: 'key', value: '' },
    }]);
    expect('prepareSquire' in capabilities.firstRun).toBe(false);
    expect(capabilities.firstRun.getSetupState).toBeUndefined();
  });

  it('provides explicit credential file selection before the shared login adapter', async () => {
    const { capabilities, calls } = callableComposition();
    expect(capabilities.agentProviderLoginFiles).toBeTypeOf('function');
    const cancelled = capabilities.agentProviderLoginFiles!('worker', 'claude');
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(calls).toEqual([]);
    const rejection = expect(cancelled).rejects.toMatchObject({ code: 'LOGIN_FILES_CANCELLED' });
    input.dispatchEvent(new Event('cancel'));
    await rejection;
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(calls).toEqual([]);
  });

  it('uses one generated client for org and chat reads and never enters the dev preview', async () => {
    const { connection, calls, capabilities } = composition();
    const user = userEvent.setup();
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities} />);

    expect(document.querySelector('main#main-content')).not.toBeNull();
    expect(document.body).not.toHaveTextContent(/部分功能暂不可用/);
    expect(screen.queryByText(/场景|DevPreview/)).not.toBeInTheDocument();
    await waitFor(() => expect(calls.map((call) => call.method)).toContain('org.list'));

    await user.click(screen.getByRole('button', { name: /^聊天/ }));
    expect(await screen.findByRole('heading', { name: '聊天' })).toBeInTheDocument();
    expect(await screen.findByTestId('chat-layout')).toBeInTheDocument();
    await waitFor(() => {
      expect(calls.filter((call) => call.method === 'message.query')).toHaveLength(2);
    });
    expect(screen.queryByRole('button', { name: /worker/ })).toBeNull();
    expect(calls.filter((call) => call.method === 'message.query').map((call) => call.params.view).sort())
      .toEqual(['inbox', 'outbox']);
    expect(screen.queryByText(/以下为隔离契约演示数据|模拟/)).not.toBeInTheDocument();
  });

  it('injects agents and settings adapters through the same connection', async () => {
    const { capabilities, calls } = composition();
    await capabilities.agents?.list?.();
    await capabilities.diagnostic?.read();
    await capabilities.messageRead?.history('c.abcdefghijklmnopqrstuvwxyz');

    expect(calls.map((call) => call.method)).toEqual(expect.arrayContaining([
      'agent.list', 'top.snapshot', 'conversations.history',
    ]));
    // A7: the boolean is derived from the served catalog, not a hard switch.
    expect(capabilities.methodAccess?.('auth.login.begin').available).toBe(true);
    // The real adapter now reaches the one GeneratedClient; this fixture's
    // invoke has no onboarding case, so the failure is honest, not "unavailable".
    await expect(capabilities.firstRun.authenticateAccount?.({ account: 'ignored', token: 'ignored' }, 'login'))
      .resolves.toMatchObject({ status: 'failed' });
    expect(calls.map((call) => call.method)).toContain('auth.login.begin');
  });

  it('does not expose device initialization when serving catalog omits it', () => {
    const connection = createBackendConnection({
      async invoke<TResult>(): Promise<TResult> {
        return { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true,
          owner: 'test@example.invalid', deviceReady: true, initialized: false } as TResult;
      },
    }, { ...DESCRIPTOR, methods: [
      { name: 'onboarding.status', callable: true, state: 'available', mutates: false },
    ] });
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.methodAccess?.('onboarding.initialize').available).toBe(false);
    expect(capabilities.firstRun.initializeDevice).toBeUndefined();
  });

  it('keeps device initialization when the served catalog includes it', () => {
    const connection = createBackendConnection({
      async invoke<TResult>(): Promise<TResult> {
        return { operations: [], nextActions: [], ok: true, phase: 'bootstrap', authenticated: true,
          owner: 'test@example.invalid', deviceReady: true, initialized: false } as TResult;
      },
    }, { ...DESCRIPTOR, methods: [
      { name: 'onboarding.status', callable: true, state: 'available', mutates: false },
      { name: 'onboarding.initialize', callable: true, state: 'available', mutates: true },
    ] });
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.methodAccess?.('onboarding.initialize').available).toBe(true);
    expect(capabilities.firstRun.initializeDevice).toBeTypeOf('function');
  });

  it('injects the device settings reads into ordinary capabilities through the shared client', async () => {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string): Promise<TResult> {
        calls.push(method);
        const values: Record<string, unknown> = {
          'daemon.status': { running: true, version: 'test', phase: 'serving' },
          'autoupdate.status': {},
          'upgrade.steps.status': { steps: [], legacyArgvFiles: [], legacyProcesses: [] },
          'log.page': { entries: [], nextCursor: null },
        };
        return values[method] as TResult;
      },
    }, DESCRIPTOR);
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.localManagement).toBeDefined();
    expect(capabilities.upgradeStatus).toBeDefined();
    expect(capabilities.logs).toBeDefined();
    await capabilities.localManagement!.status();
    await capabilities.upgradeStatus!.status();
    await capabilities.upgradeStatus!.steps!();
    await capabilities.logs!.read();
    expect(calls.sort()).toEqual([
      'autoupdate.status', 'daemon.status', 'log.page', 'upgrade.steps.status',
    ]);
  });

  it('wires chat send and targets from the same client with host session display scope', async () => {
    sessionStorage.clear();
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const actor = 'user:owner';
    const target = 'agent:owner:node:other';
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        const input = params as Record<string, unknown>;
        calls.push({ method, params: input });
        if (method === 'targets') return { targets: [{ targetUri: target, targetKind: 'agent',
          deliverable: true, actor: target, status: 'online' }] } as TResult;
        if (method === 'message.send') return { ok: true, operationId: input.idempotencyKey,
          conversationId: input.conversationId, messageId: 'sent-1',
          deliveries: [{ target, messageId: 'sent-1' }] } as TResult;
        throw new Error('unexpected method ' + method);
      },
    }, { ...DESCRIPTOR, principalActor: actor });
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.chatSend).toBeDefined();
    expect(capabilities.chatSession).toEqual({ actor, generation: DESCRIPTOR.generation });
    expect(await capabilities.chatTargets!('agent')).toHaveLength(1);
    await expect(capabilities.chatSend!.send({ to: [target], message: 'test', conversationId: 'existing' }))
      .resolves.toMatchObject({ status: 'accepted' });
    expect(calls.map(call => call.method)).toEqual(['targets', 'message.send']);
    expect(calls[1].params).not.toHaveProperty('actor');
    expect(calls[1].params).not.toHaveProperty('principal');
    connection.retire();
    await expect(capabilities.chatTargets!('agent')).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RETIRED' });
    await expect(capabilities.chatSend!.send({ to: [target], message: 'test', conversationId: 'existing' }))
      .resolves.toMatchObject({ status: 'not-started' });
    expect(calls).toHaveLength(2);
    sessionStorage.clear();
  });

  it('admits cataloged writes through shared adapters while retaining reads and rejecting absent methods', async () => {
    const { calls, capabilities } = callableComposition();
    for (const method of [
      'agent.list', 'agent.create', 'agent.start', 'agent.provider.login', 'agent.provider.default',
      'org.create', 'org.fetch', 'adapter.list', 'adapter.start', 'adapter.stop', 'adapter.reload',
    ]) {
      expect(capabilities.methodAccess?.(method)).toMatchObject({ available: true });
    }
    expect(capabilities.methodAccess?.('agent.stop')).toMatchObject({
      available: false, code: 'METHOD_NOT_FOUND',
    });
    expect(capabilities.methodAccess?.('workflow.list')).toMatchObject({
      available: false, code: 'METHOD_NOT_FOUND',
    });

    await expect(capabilities.agents?.start?.('worker')).resolves.toMatchObject({
      accepted: true, running: true,
    });
    await expect(capabilities.messageRead?.query('outbox')).resolves.toEqual([]);
    expect(calls.filter((call) => call.method === 'agent.start')).toHaveLength(1);
    expect(calls.filter((call) => call.method === 'message.query')).toHaveLength(1);
    await expect(capabilities.agents?.defaultProvider?.()).resolves.toEqual({ ready: true, provider: null });
    expect(calls.filter((call) => call.method === 'agent.provider.default').map((call) => call.params))
      .toEqual([{}]);
  });

  it('surfaces a missing catalog method before dispatch while retaining reads', async () => {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string): Promise<TResult> {
        calls.push(method);
        if (method === 'agent.list') return { agents: [] } as TResult;
        throw new Error(`unexpected dispatch ${method}`);
      },
    }, {
      ...DESCRIPTOR,
      methods: [
        { name: 'agent.list', callable: true, state: 'available', mutates: false },
      ],
    });
    const capabilities = createConnectedGuiCapabilities(connection);
    expect(capabilities.methodAccess?.('agent.create')).toMatchObject({
      available: false, code: 'METHOD_NOT_FOUND',
    });
    await expect(capabilities.agents?.create?.({
      name: 'blocked', provider: null, model: null, preferredHarness: null,
    })).rejects.toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched' });
    await expect(capabilities.agents?.list?.()).resolves.toEqual([]);
    expect(calls).toEqual(['agent.list']);
  });

  it('keeps channel reads and enables cataloged channel writes', async () => {
    const { connection, calls, capabilities } = callableComposition();
    const user = userEvent.setup();
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities} />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    expect(await screen.findByText('暂无平台接入')).toBeInTheDocument();
    const platform = screen.getByRole('heading', { name: '平台接入' }).closest('section') as HTMLElement;
    expect(within(platform).getByRole('button', { name: '刷新' })).toBeEnabled();
    expect(calls.some((call) => call.method === 'adapter.list')).toBe(true);
    expect(calls.filter((call) => ['adapter.start', 'adapter.stop', 'adapter.reload'].includes(call.method)))
      .toHaveLength(0);
  });

  it('composes connected adapters from the injected connection when capabilities are omitted', async () => {
    const { connection, calls } = composition();
    const user = userEvent.setup();
    render(<App backend={{ status: 'connected', connection }} />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
    expect(await screen.findByText('暂无平台接入')).toBeInTheDocument();
    expect(calls.some((call) => call.method === 'adapter.list')).toBe(true);
  });

  it('does not fall back to the legacy org fixture in the ordinary connected shell', async () => {
    const { connection, calls } = composition();
    const user = userEvent.setup();
    render(
      <App
        backend={{ status: 'connected', connection }}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^组织/ }));
    await waitFor(() => expect(calls.some((call) => call.method === 'org.list')).toBe(true));
    expect(screen.queryByText(/演示组织|模拟数据/)).not.toBeInTheDocument();
  });

  it('shows the refused request id without exposing the upstream envelope', async () => {
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        if (method === 'message.query') {
          throw new HttpError('UPSTREAM_UNAVAILABLE', 'selected backend is unavailable', 502, {
            method,
            requestId: 'request-refused-42',
            status: 502,
            dispatch: 'not-dispatched',
            catalogDigest: DESCRIPTOR.catalogDigest,
            attempts: 1,
            startedAt: 1,
            durationMs: 1,
          });
        }
        return resultFor(method, params as Record<string, unknown>) as TResult;
      },
    }, DESCRIPTOR);
    const user = userEvent.setup();
    render(
      <App
        backend={{ status: 'connected', connection }}
        capabilities={createConnectedGuiCapabilities(connection)}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^聊天/ }));
    expect(await screen.findByText(/request-refused-42/)).toBeInTheDocument();
    expect(screen.getByText(/UPSTREAM_UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.queryByText(/upstream-secret|authorization/i)).not.toBeInTheDocument();
  });

  it('does not consume native account globals without an explicit capability', async () => {
    const status = vi.fn(async () => ({
      authenticated: false,
      owner: null,
      deviceReady: false,
      sidecarError: null,
      ready: false,
    }));
    Object.defineProperty(window, '__hyprialAccount', {
      configurable: true,
      value: {
        status,
        login: vi.fn(),
        device: vi.fn(),
        cancel: vi.fn(),
      },
    });
    try {
      render(<App />);
      expect(status).not.toHaveBeenCalled();
    } finally {
      delete (window as unknown as Record<string, unknown>).__hyprialAccount;
    }
  });
});

describe('chat sources follow the served catalog', () => {
  function served(methods: readonly string[]) {
    const calls: string[] = [];
    const connection = createBackendConnection({
      async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
        calls.push(method);
        return resultFor(method, params as Record<string, unknown>) as TResult;
      },
    }, {
      ...DESCRIPTOR,
      methods: ['onboarding.status', ...methods].map(name => ({ name, callable: true, state: 'available', mutates: false })),
    });
    return { connection, calls, capabilities: createConnectedGuiCapabilities(connection) };
  }

  it('hands down the target port only when targets is callable', () => {
    expect(served(['message.query']).capabilities.chatTargets).toBeUndefined();
    expect(served(['targets']).capabilities.chatTargets).toBeTypeOf('function');
  });

  it('hands down the message read port only when one of its page reads is callable', () => {
    expect(served(['targets']).capabilities.messageRead).toBeUndefined();
    for (const method of ['message.query', 'conversations.list']) {
      expect(served([method]).capabilities.messageRead).toBeDefined();
    }
    expect(served(['conversations.history']).capabilities.messageRead).toBeUndefined();
  });

  it('hides chat with only the history read callable and renders no false empty directory', async () => {
    const { connection, capabilities } = served(['targets', 'conversations.history']);
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities}
      initialState={{ ...createEmptyState('complete'), page: 'chat' }} />);
    expect(screen.queryByRole('button', { name: /^聊天/ })).not.toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-layout')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('暂无聊天对象');
  });

  it('has no chat navigation entry and lands the chat route on start without a target source', async () => {
    const { connection, capabilities } = served(['message.query']);
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities}
      initialState={{ ...createEmptyState('complete'), page: 'chat' }} />);
    expect(screen.queryByRole('button', { name: /^聊天/ })).not.toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-layout')).not.toBeInTheDocument();
  });

  it('has no chat navigation entry without a message read source', () => {
    const { connection, capabilities } = served(['targets']);
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities} />);
    expect(screen.queryByRole('button', { name: /^聊天/ })).not.toBeInTheDocument();
  });

  it('shows the chat page when both sources are callable and the history read is the only refused one', async () => {
    const { connection, calls, capabilities } = served(['targets', 'message.query']);
    const user = userEvent.setup();
    render(<App backend={{ status: 'connected', connection }} capabilities={capabilities} />);
    await user.click(screen.getByRole('button', { name: /^聊天/ }));
    expect(await screen.findByTestId('chat-layout')).toBeInTheDocument();
    await waitFor(() => expect(calls).toContain('targets'));
    expect(calls).not.toContain('conversations.history');
  });
});
