/*
 * Decision record (unavailable copy to notifications, PR 1f/F1, 2026-11-14):
 * - Agents area under the owner rule: no source -> nothing rendered (nav entry,
 *   tabs, controls); a call the catalog refuses -> the control stays enabled and
 *   the refusal is notified when the user operates it, with no side effect
 *   started and no inline sentence.
 * - Refusals reach the handler as something methodRefusal classifies; the
 *   catalog reason sentence never reaches the page.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useReducer, type ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { AppShell } from '../../../src/app/AppShell';
import { GuiCapabilitiesProvider, type GuiCapabilities } from '../../../src/app/capabilities';
import { MethodRefusedError, methodRefusal, type MethodAccess } from '../../../src/client';
import { ToastHost } from '../../../src/components/ToastHost';
import { NotificationsProvider } from '../../../src/components/notifications';
import { createFixtureState } from '../../../src/dev/fixtures/fixture-state';
import { contractExamples } from '../../../src/dev/contract-mock';
import type { AgentDestroyPreviewResult } from '../../../src/client/generated/types';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { createLocalAgentGateway, type AgentsApi } from '../../../src/features/agents';
import { AgentForm } from '../../../src/features/agents/impl/AgentForm';
import { createAgentsApi } from '../../../src/features/agents/impl/api';
import { createAgentAdapterBindingsFromClient } from '../../../src/features/agents/impl/api/agent-adapter-bindings';
import { CredentialsTab } from '../../../src/features/agents/impl/detail/CredentialsTab';
import { OverviewTab } from '../../../src/features/agents/impl/detail/OverviewTab';
import type { SharedCredentialsApi } from '../../../src/features/connections/shared-credentials-api';
import { appReducer } from '../../../src/state/appReducer';
import { createGeneratedClient } from '../../../src/client/generated';
import { authenticatedTestActions, DEMO_BASELINE } from '../../support/demo-baseline';
import { AGENTS_LIST_ROWS_DATA } from '../../fixtures/agents/agent-list';
import { agentsTestClient, createTestAgentsApi } from '../../unified-backend/agents-test-client';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const BANNED = /暂不可用|尚未开放|不可用|未接入|暂不支持|METHOD_NOT_|GUI_CAPABILITY_UNAVAILABLE|当前会话只读/;

function pageText(): string {
  const clone = document.body.cloneNode(true) as HTMLElement;
  clone.querySelectorAll('.toast-host, [data-testid="org-write-unavailable"]').forEach((node) => node.remove());
  return clone.textContent ?? '';
}

interface Call {
  readonly method: string;
  readonly params: Record<string, unknown>;
}

function directoryApi(access?: MethodAccess, handlers: Readonly<Record<string, () => unknown>> = {}) {
  const calls: Call[] = [];
  const api = createTestAgentsApi({
    async request(method, params) {
      calls.push({ method, params });
      const handler = handlers[method];
      if (handler) return handler();
      if (method === 'agent.list') return { ok: true, agents: AGENTS_LIST_ROWS_DATA, agentsWithoutRoutine: [] };
      if (method === 'agent.secret.list') {
        return { ok: true, actor: 'a', agent: 'a', agentId: 'a', grants: [] };
      }
      if (method === 'agent.stop') return { ok: true, removed: ['claude:researcher'] };
      throw new Error(`unexpected method ${method}`);
    },
  }, access);
  return { api, calls };
}

function refuse(...methods: readonly string[]): MethodAccess {
  return (method) => (methods.includes(method)
    ? { available: false, code: 'METHOD_NOT_CALLABLE', reason: '目录声明该方法不可调用。' }
    : { available: true });
}

function renderDirectory(api: ReturnType<typeof directoryApi>['api'], extra: Partial<GuiCapabilities> = {}) {
  const user = userEvent.setup();
  render(
    <FixtureApp
      dataMode="fixture"
      capabilities={{ firstRun: authenticatedTestActions(), agents: api, ...extra }}
    />,
  );
  return { user };
}

async function openDetail(user: ReturnType<typeof userEvent.setup>, id = 'contact-researcher') {
  await user.click(screen.getAllByRole('button', { name: 'Agent' })[0]);
  await user.click(await screen.findByTestId(id));
  return await screen.findByTestId('agent-detail-panel');
}

function toastTexts(): (string | null)[] {
  return Array.from(document.querySelectorAll('.toast.toast-warning .toast-text')).map((node) => node.textContent);
}

describe('Agent destination follows its source', () => {
  function Shell({ capabilities, seed, gateway }: {
    readonly capabilities?: GuiCapabilities;
    readonly seed?: ReturnType<typeof seedOf>;
    readonly gateway?: ReturnType<typeof createLocalAgentGateway>;
  }) {
    const [state, dispatch] = useReducer(appReducer, undefined, () => ({
      ...createFixtureState(DEMO_BASELINE),
      page: 'contacts' as const,
    }));
    return (
      <GuiCapabilitiesProvider value={capabilities ?? { firstRun: authenticatedTestActions() }}>
        <AppShell state={state} dispatch={dispatch} firstRunWizardEnabled={false}
          agentSeed={seed} agentGateway={gateway} />
      </GuiCapabilitiesProvider>
    );
  }

  function seedOf() {
    return { agents: [], connections: [], organizations: [] };
  }

  it('hides the Agent entry and routes to the start page when the composition has no agent source', async () => {
    render(<Shell />);
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull();
    expect(document.querySelector('[data-nav="agents"]')).toBeNull();
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1, name: 'Agent' })).toBeNull();
  });

  it.each([
    ['a directory api', () => ({ capabilities: { firstRun: authenticatedTestActions(), agents: directoryApi().api } })],
    ['an injected seed', () => ({ seed: seedOf() })],
    ['an injected gateway', () => ({ gateway: createLocalAgentGateway({ agents: [], connections: [], organizations: [] }) })],
  ] as const)('keeps the Agent entry and page with %s', async (_name, props) => {
    render(<Shell {...props()} />);
    expect(screen.getByRole('button', { name: 'Agent' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('heading', { level: 1, name: '开始' })).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });
});

describe('runtime controls with a refused method', () => {
  it('start: the control stays enabled, notifies on use and starts nothing', async () => {
    const { api, calls } = directoryApi(refuse('agent.start'));
    const { user } = renderDirectory(api, { methodAccess: refuse('agent.start') });
    const detail = await openDetail(user, 'contact-writer');

    const start = within(detail).getByRole('button', { name: '启动' });
    expect(start).toBeEnabled();
    expect(start).not.toHaveAttribute('title');
    await user.click(start);

    await waitFor(() => expect(toastTexts()).toEqual(['启动 Agent暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '启动 Agent' })).toBeNull();
    expect(within(detail).getByRole('button', { name: '启动' })).toBeEnabled();
    expect(calls.some((call) => call.method === 'agent.start')).toBe(false);
    expect(detail.querySelector('[data-runtime="header-result"]')).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });

  it('stop: the control stays enabled, notifies on use and sends no request', async () => {
    const { api, calls } = directoryApi(refuse('agent.stop'));
    const { user } = renderDirectory(api, { methodAccess: refuse('agent.stop') });
    const detail = await openDetail(user, 'contact-squire');

    const stop = within(detail).getByRole('button', { name: '停止' });
    expect(stop).toBeEnabled();
    expect(stop).not.toHaveAttribute('title');
    await user.click(stop);

    await waitFor(() => expect(toastTexts()).toEqual(['停止 Agent暂不可用。']));
    expect(within(detail).getByRole('button', { name: '停止' })).toBeEnabled();
    expect(calls.some((call) => call.method === 'agent.stop')).toBe(false);
    expect(detail.querySelector('[data-runtime="header-result"]')).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });

  it('an injected client without the stop handler has no start or stop control and no notification', async () => {
    const { client } = agentsTestClient(async (method) => {
      if (method === 'agent.list') return { ok: true, agents: AGENTS_LIST_ROWS_DATA, agentsWithoutRoutine: [] };
      if (method === 'agent.secret.list') return { ok: true, actor: 'a', agent: 'a', agentId: 'a', grants: [] };
      throw new Error(`unexpected method ${method}`);
    });
    const api = createAgentsApi({ ...client, agent: { ...client.agent, stop: undefined } } as never);
    const { user } = renderDirectory(api);
    const detail = await openDetail(user, 'contact-squire');

    expect(api.stop).toBeUndefined();
    expect(within(detail).queryByRole('button', { name: '停止' })).toBeNull();
    expect(within(detail).queryByRole('button', { name: '启动' })).toBeNull();
    expect(toastTexts()).toEqual([]);
    expect(document.body.textContent).not.toMatch(/GUI_CAPABILITY_UNAVAILABLE|Agent 能力暂不可用/);
  });

  it('rename: notifies once and does not open the editor, with no request', async () => {
    const { api, calls } = directoryApi(refuse('agent.rename'));
    const { user } = renderDirectory(api, { methodAccess: refuse('agent.rename') });
    const detail = await openDetail(user);

    await user.click(within(detail).getByRole('button', { name: '重命名' }));

    await waitFor(() => expect(toastTexts()).toEqual(['重命名 Agent暂不可用。']));
    expect(within(detail).queryByLabelText('Agent 名称')).toBeNull();
    expect(within(detail).getByRole('button', { name: '重命名' })).toBeEnabled();
    expect(detail.querySelector('.agent-field-error')).toBeNull();
    expect(calls.some((call) => call.method === 'agent.rename')).toBe(false);
    expect(pageText()).not.toMatch(BANNED);
  });
});

describe('delete', () => {
  it.each(['previewDestroy', 'destroy'] as const)('hides deletion without %s even if legacy deletion exists', async (missing) => {
    const { api } = directoryApi();
    const optional: AgentsApi = api;
    optional[missing] = undefined;
    const legacy = vi.fn();
    const { user } = renderDirectory(api, { agentDelete: legacy });
    const detail = await openDetail(user);
    expect(within(detail).queryByRole('button', { name: '删除' })).toBeNull();
    expect(legacy).not.toHaveBeenCalled();
    expect(pageText()).not.toMatch(BANNED);
  });

  it.each(['agent.destroy.preview', 'agent.destroy'])('refuses %s before opening confirmation or sending requests', async (method) => {
    const { api, calls } = directoryApi(refuse(method));
    const { user } = renderDirectory(api, { methodAccess: refuse(method) });
    const detail = await openDetail(user);
    await user.click(within(detail).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(toastTexts()).toEqual(['删除 Agent暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '删除 Agent？' })).toBeNull();
    expect(calls.some(call => call.method.startsWith('agent.destroy'))).toBe(false);
    expect(within(detail).getByRole('button', { name: '删除' })).toBeEnabled();
    expect(pageText()).not.toMatch(BANNED);
  });

  it.each(['preview', 'execute'])('a late %s refusal closes confirmation, notifies and preserves the Agent', async (phase) => {
    const { api } = directoryApi();
    const preview = contractExamples('agent.destroy.preview').find(row => row.name === 'ready')!.result as unknown as AgentDestroyPreviewResult;
    api.previewDestroy = vi.fn(async () => {
      if (phase === 'preview') throw new MethodRefusedError('agent.destroy.preview', 'METHOD_NOT_CALLABLE');
      return preview;
    });
    api.destroy = vi.fn(async () => { throw new MethodRefusedError('agent.destroy', 'METHOD_NOT_CALLABLE'); });
    const { user } = renderDirectory(api);
    const detail = await openDetail(user);
    await user.click(within(detail).getByRole('button', { name: '删除' }));
    if (phase === 'execute') {
      await screen.findByText(preview.workspace.path);
      await user.click(within(screen.getByRole('dialog', { name: '删除 Agent？' })).getByRole('button', { name: '确认删除' }));
    }
    await waitFor(() => expect(toastTexts()).toEqual(['删除 Agent暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '删除 Agent？' })).toBeNull();
    expect(screen.getByTestId('agent-detail-panel')).toBeInTheDocument();
    expect(within(detail).getByRole('button', { name: '删除' })).toBeEnabled();
    expect(api.previewDestroy).toHaveBeenCalledTimes(1);
    expect(api.destroy).toHaveBeenCalledTimes(phase === 'execute' ? 1 : 0);
    expect(pageText()).not.toMatch(BANNED);
  });
});

describe('overview jumps only to tabs that exist', () => {
  it('offers no jump to a tab the detail does not have', async () => {
    const { api } = directoryApi();
    api.providerStatus = async () => [{ grantId: 'g', provider: 'anthropic', state: 'recipient-pending',
      code: null, keyId: null, owner: null, expiresAtMs: null, unsealable: false }];
    const { user } = renderDirectory(api);
    const detail = await openDetail(user);
    const notices = await within(detail).findByTestId('overview-notices');
    await within(notices).findByText(/等待原主人设备/);
    expect(within(notices).getByRole('button', { name: /去凭据/ })).toBeInTheDocument();
    expect(within(notices).queryByRole('button', { name: /去所属组织/ })).toBeNull();
  });

  it('suppresses jump controls when OverviewTab is told a tab does not exist', () => {
    render(
      <OverviewTab
        availableTabs={['overview']}
        onJumpTo={vi.fn()}
        notices={[
          {
            key: 'missing-tab',
            tone: 'warning',
            chip: '提示',
            text: '需要查看所属组织',
            jumpTo: 'relations',
          },
        ]}
      />,
    );
    const notices = screen.getByTestId('overview-notices');
    expect(within(notices).getByText('需要查看所属组织')).toBeInTheDocument();
    expect(within(notices).queryByRole('button', { name: /去所属组织/ })).toBeNull();
  });
});

describe('credentials', () => {
  function credentials(overrides: Partial<SharedCredentialsApi> = {}): SharedCredentialsApi {
    return {
      listKeys: async () => [{
        keyId: 'one:ANTHROPIC_API_KEY', provider: 'anthropic', label: '一号 Key', state: 'active', createdAt: 1, lastUsedAt: null, grants: [],
      }],
      listSecretGrants: async () => [],
      addKey: async () => ({ status: 'failed', message: 'unused' }),
      replaceKey: async () => ({ status: 'failed', message: 'unused' }),
      deleteKey: async () => ({ status: 'failed', message: 'unused' }),
      revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
      ...overrides,
    };
  }

  function Host({ children }: { readonly children: ReactNode }) {
    const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
    return (
      <NotificationsProvider dispatch={dispatch}>
        {children}
        <ToastHost notices={state.notices} dispatch={dispatch} />
      </NotificationsProvider>
    );
  }

  const READY = { view: { status: 'ready' as const, rows: [] }, reload: () => {} };

  it('a refused save notifies, keeps the editor open and enabled, and shows nothing inline', async () => {
    const { api, calls } = directoryApi(refuse('agent.secret.grant'));
    const user = userEvent.setup();
    render(
      <Host>
        <CredentialsTab active actor="agent:alice:node:worker" preferredHarness="claude" expectedAgent={null}
          providerStatus={READY} agentsApi={api} credentialsApi={credentials()} onNavigateConnections={() => {}} />
      </Host>,
    );
    await user.click(await screen.findByRole('button', { name: '编辑凭据' }));
    await user.click(await screen.findByRole('button', { name: '保存' }));

    await waitFor(() => expect(toastTexts()).toEqual(['保存凭据暂不可用。']));
    const dialog = screen.getByRole('dialog', { name: '编辑凭据' });
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled();
    expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(calls.some((call) => call.method === 'agent.secret.grant')).toBe(false);
    expect(pageText()).not.toMatch(BANNED);
  });

  it('renders no 编辑凭据 without a write port, and no picker button without a picker', async () => {
    const user = userEvent.setup();
    const first = render(
      <CredentialsTab active actor="a" expectedAgent={null} providerStatus={READY} onNavigateConnections={() => {}} />,
    );
    expect(screen.queryByRole('button', { name: '编辑凭据' })).toBeNull();
    first.unmount();
    const { api } = directoryApi();
    render(
      <CredentialsTab active actor="a" preferredHarness="claude" expectedAgent={null} providerStatus={READY}
        agentsApi={api} credentialsApi={credentials()} onNavigateConnections={() => {}} />,
    );
    await user.click(await screen.findByRole('button', { name: '编辑凭据' }));
    await user.click(await screen.findByRole('combobox', { name: '凭据' }));
    await user.click(await screen.findByRole('option', { name: 'Auth' }));
    expect(screen.queryByRole('button', { name: /选择.*登录文件/ })).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });

  it('a refused Key list read offers no Key and shows no sentence', async () => {
    const { api } = directoryApi();
    const user = userEvent.setup();
    render(
      <CredentialsTab active actor="a" preferredHarness="claude" expectedAgent={null} providerStatus={READY}
        agentsApi={api}
        credentialsApi={credentials({ listKeys: async () => { throw new MethodRefusedError('agent.secret.provider-list', 'METHOD_NOT_AVAILABLE'); } })}
        nativeLoginFiles={async () => []}
        onNavigateConnections={() => {}} />,
    );
    await user.click(await screen.findByRole('button', { name: '编辑凭据' }));
    await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(screen.queryByRole('combobox', { name: '凭据' })).toBeNull();
    expect(screen.queryByRole('combobox', { name: 'Key' })).toBeNull();
    expect(screen.getByLabelText('harness')).toBeInTheDocument();
    expect(screen.getByText((text) => text.includes('~/.claude/.credentials.json'))).toBeVisible();
    expect(pageText()).not.toMatch(BANNED);
  });
});

describe('create form offers only the credentials it has ports for', () => {
  const base = {
    mode: 'create' as const,
    initial: { displayName: '', role: '', modelServiceId: '', organizationId: null },
    pending: false,
    message: null,
    onSubmit: () => {},
    onCancel: () => {},
  };
  const keys: SharedCredentialsApi = {
    listKeys: async () => [{ keyId: 'one:ANTHROPIC_API_KEY', provider: 'anthropic', label: '一号 Key', state: 'active', createdAt: 1, lastUsedAt: null, grants: [] }],
    listSecretGrants: async () => [],
    addKey: async () => ({ status: 'failed', message: 'unused' }),
    replaceKey: async () => ({ status: 'failed', message: 'unused' }),
    deleteKey: async () => ({ status: 'failed', message: 'unused' }),
    revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
  };

  it('renders no 凭据 field when neither a key list nor a file picker exists', () => {
    render(<AgentForm {...base} />);
    expect(screen.queryByLabelText('凭据')).toBeNull();
    expect(screen.queryByLabelText('Key')).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });

  it('submits an empty credential plan when there is nothing to choose', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<AgentForm {...base} onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText('名称'), 'worker');
    await user.click(screen.getByLabelText('harness'));
    await user.click(await screen.findByRole('option', { name: 'claude' }));
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ credential: { kind: 'none' } }));
  });

  it('offers Key only with a key list, Auth only with a picker', async () => {
    const user = userEvent.setup();
    const first = render(<AgentForm {...base} credentialsApi={keys} />);
    await user.click(screen.getByLabelText('harness'));
    await user.click(await screen.findByRole('option', { name: 'claude' }));
    await user.click(await screen.findByLabelText('凭据'));
    expect(await screen.findByRole('option', { name: 'Key' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Auth' })).toBeNull();
    first.unmount();
    render(<AgentForm {...base} pickLoginFiles={async () => []} />);
    await user.click(screen.getByLabelText('凭据'));
    expect(await screen.findByRole('option', { name: 'Auth' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Key' })).toBeNull();
  });

  it('a refused key list read removes the Key option without a sentence', async () => {
    const user = userEvent.setup();
    render(<AgentForm {...base} pickLoginFiles={async () => []}
      credentialsApi={{ ...keys, listKeys: async () => { throw new MethodRefusedError('agent.secret.provider-list', 'METHOD_NOT_AVAILABLE'); } }} />);
    await user.click(screen.getByLabelText('凭据'));
    expect(await screen.findByRole('option', { name: 'Auth' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Key/ })).toBeNull();
    expect(pageText()).not.toMatch(BANNED);
  });
});

describe('refusals reach the handler in a classifiable form', () => {
  const always: MethodAccess = (method) => ({ available: false, code: 'METHOD_NOT_AVAILABLE', reason: `${method} 的目录原因句` });

  async function failure(run: () => Promise<unknown>): Promise<unknown> {
    return await run().then(() => null, (error: unknown) => error);
  }

  it.each([
    ['list', (api: Required<AgentsApi>) => api.list(), 'agent.list'],
    ['create', (api: Required<AgentsApi>) => api.create({ name: 'w', provider: null, model: null, preferredHarness: null }), 'agent.create'],
    ['start', (api: Required<AgentsApi>) => api.start('w'), 'agent.start'],
    ['stop', (api: Required<AgentsApi>) => api.stop('w'), 'agent.stop'],
    ['rename', (api: Required<AgentsApi>) => api.rename('w', 'x'), 'agent.rename'],
    ['providerStatus', (api: Required<AgentsApi>) => api.providerStatus('w', null), 'agent.secret.list'],
    ['loginNative', (api: Required<AgentsApi>) => api.loginNative('w', 'claude', []), 'agent.provider.login'],
    ['logoutNative', (api: Required<AgentsApi>) => api.logoutNative('w', 'claude'), 'agent.provider.logout'],
  ] as const)('agents api %s', async (_name, call, method) => {
    const { client, calls } = agentsTestClient(async () => ({ ok: true }));
    const error = await failure(() => call(createAgentsApi(client, always)));
    expect(methodRefusal(error)).toEqual({ code: 'METHOD_NOT_AVAILABLE', source: method });
    expect((error as Error).message).toBe(`METHOD_NOT_AVAILABLE: ${method}`);
    expect(calls).toEqual([]);
  });

  it('adapter bindings and route calls', async () => {
    const { client, calls } = agentsTestClient(async () => ({ ok: true }));
    const api = createAgentAdapterBindingsFromClient(client as unknown as ReturnType<typeof createGeneratedClient>, always);
    for (const [run, method] of [
      [() => api.list(), 'adapter.list'],
      [() => api.bind('a', 'b'), 'agent.cell.get'],
      [() => api.unbind('a', 'b'), 'agent.cell.get'],
      [() => api.routes.api!.list('a'), 'adapter.routes.list'],
      [() => api.routes.api!.candidates('a'), 'adapter.routes.candidates'],
      [() => api.routes.api!.add('a', 'r', 'c', false, 'rev'), 'adapter.routes.add'],
      [() => api.routes.api!.remove('a', 'r', false, 'rev'), 'adapter.routes.remove'],
      [() => api.routes.api!.reload(), 'adapter.reload'],
    ] as const) {
      const error = await failure(run);
      expect(methodRefusal(error)).toEqual({ code: 'METHOD_NOT_AVAILABLE', source: method });
      expect((error as Error).message).toBe(`METHOD_NOT_AVAILABLE: ${method}`);
    }
    expect(calls).toEqual([]);
  });

  it('a dispatched failure is not a refusal', async () => {
    const { client } = agentsTestClient(async () => { throw new Error('daemon unreachable'); });
    const error = await failure(() => createAgentsApi(client).stop('w'));
    expect(methodRefusal(error)).toBeNull();
  });
});
