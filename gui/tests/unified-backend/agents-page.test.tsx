/** Generated contract test doubles and shared ContactsPage; not live browser/backend evidence. */
import { useReducer } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GuiCapabilitiesProvider } from '../../src/app/capabilities';
import { ToastHost } from '../../src/components/ToastHost';
import { NotificationsProvider } from '../../src/components/notifications';
import { expectUnavailableToast } from '../support/notify-harness';
import { ContactsPage } from '../../src/pages/ContactsPage';
import { appReducer } from '../../src/state/appReducer';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import type { AppState } from '../../src/state/types';
import { buildAgentScenario } from '../../src/dev/j2-scenarios';
import { createAgentsApi, type AgentsApi } from '../../src/features/agents/impl/api';
import type { AgentMutationGateway } from '../../src/features/agents/impl/AgentMutationGateway';
import type { MethodAccess } from '../../src/client';
import { AGENTS_LIST_ROWS_DATA } from '../fixtures/agents/agent-list';
import { CREATE_NEW_AGENT } from '../fixtures/agents/agent-mutations';
import { agentsTestClient } from './agents-test-client';
import { DEMO_BASELINE } from '../support/demo-baseline';
import { stubFirstRunActions } from '../support/first-run-actions';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const seed = buildAgentScenario('m04')!.seed;
const firstRun = stubFirstRunActions();

function Page({ api, panel = { kind: 'list' }, injectedSeed = false, gateway, methodAccess }: {
  api?: AgentsApi; panel?: AppState['agentPanel'];
  injectedSeed?: boolean; gateway?: AgentMutationGateway; methodAccess?: MethodAccess;
}) {
  const [state, dispatch] = useReducer(appReducer, {
    ...createFixtureState(DEMO_BASELINE), page: 'contacts', agentPanel: panel,
  });
  return <GuiCapabilitiesProvider value={{
    firstRun,
    agents: api,
    sharedCredentials: api === undefined
      ? undefined
      : {
          listKeys: async () => ([{
              keyId: 'pk.test',
              provider: 'anthropic',
              label: '测试 Key',
              state: 'active',
              createdAt: 1,
              lastUsedAt: null,
              grants: [],
            }]),
          listSecretGrants: async () => [],
          addKey: async () => ({ status: 'failed', message: 'unused' }),
          replaceKey: async () => ({ status: 'failed', message: 'unused' }),
          deleteKey: async () => ({ status: 'failed', message: 'unused' }),
          revokeGrant: async () => ({ status: 'failed', message: 'unused' }),
        },
    methodAccess,
  }}>
    <NotificationsProvider dispatch={dispatch}>
      <ContactsPage state={state} dispatch={dispatch}
        seed={injectedSeed ? seed : undefined} gateway={gateway} />
      <ToastHost notices={state.notices} dispatch={dispatch} />
    </NotificationsProvider>
  </GuiCapabilitiesProvider>;
}

function testApi() {
  const result = agentsTestClient(async method => method === 'agent.list'
    ? { ok: true, agents: AGENTS_LIST_ROWS_DATA, agentsWithoutRoutine: [] }
    : method === 'agent.create' ? CREATE_NEW_AGENT.result
    : method === 'agent.provider.allow'
      ? { ok: true, changed: true, grant: { provider: 'anthropic', grantId: 'anthropic' } }
    : method === 'agent.start'
      ? { ok: true, accepted: true, running: true }
    : method === 'agent.secret.list' ? { ok: true, grants: [] }
    : { ok: true, provider: null, previous: null });
  return { ...result, api: createAgentsApi(result.client) };
}

describe('shared agent page source boundaries', () => {
  it('does not turn an absent capability into default state fixtures', () => {
    render(<Page />);
    expect(screen.queryAllByTestId(/^contact-/)).toHaveLength(0);
    expect(screen.queryAllByRole('button', { name: /创建 Agent/ })).toHaveLength(0);
    expect(document.body.textContent).not.toMatch(/暂不可用|尚未开放/);
  });

  it('keeps create enabled when the catalog refuses it and notifies on use without a request', async () => {
    const { api, calls } = testApi();
    const user = userEvent.setup();
    render(<Page api={api} methodAccess={(method) => method === 'agent.create'
      ? { available: false, code: 'METHOD_NOT_CALLABLE', reason: '目录声明 agent.create 不可调用。' }
      : { available: true }} />);
    const button = await screen.findByRole('button', { name: '创建 Agent' });
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute('title');
    expect(button).not.toHaveAttribute('aria-label');
    await waitFor(() => expect(calls.some((call) => call.method === 'agent.list')).toBe(true));
    await user.click(button);
    expectUnavailableToast('创建 Agent暂不可用。');
    expect(document.body.textContent).not.toContain('目录声明 agent.create 不可调用。');
    expect(screen.getByRole('heading', { level: 1, name: 'Agent' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: '创建 Agent' })).toBeNull();
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
    expect(calls.filter((call) => call.method === 'agent.create')).toHaveLength(0);
  });

  it('a detail panel without any agent source renders neither an agent nor a not-found claim', () => {
    render(<Page injectedSeed={false} panel={{ kind: 'detail', agentId: 'agent-squire' }} />);
    expect(screen.queryByTestId('agent-detail-panel')).toBeNull();
    expect(screen.queryByText('找不到该 Agent')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/GUI_CAPABILITY_UNAVAILABLE|暂不可用/);
  });

  it('uses the current client when composition changes while the create form is mounted', async () => {
    const first = testApi();
    const second = testApi();
    const view = render(<Page api={first.api} panel={{ kind: 'create' }} />);
    view.rerender(<Page api={second.api} panel={{ kind: 'create' }} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('名称'), 'new-worker');
    await user.click(screen.getByLabelText('harness'));
    await user.click(screen.getByRole('option', { name: 'claude' }));
    await user.click(screen.getByLabelText('凭据', { exact: true }));
    await user.click(screen.getByRole('option', { name: '原生 Auth' }));
    await user.type(screen.getByLabelText('模型 ID'), 'claude-test-model');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(first.calls.filter(call => call.method === 'agent.create')).toHaveLength(0);
    expect(second.calls.filter(call => call.method === 'agent.create')).toHaveLength(1);
  });

  it('uses a newly supplied client after a composition without a way to create', async () => {
    const current = testApi();
    const view = render(<Page panel={{ kind: 'create' }} />);
    expect(screen.queryByLabelText('名称')).toBeNull();
    view.rerender(<Page api={current.api} panel={{ kind: 'create' }} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('名称'), 'new-worker');
    await user.click(screen.getByLabelText('harness'));
    await user.click(screen.getByRole('option', { name: 'claude' }));
    await user.click(screen.getByLabelText('凭据', { exact: true }));
    await user.click(screen.getByRole('option', { name: '原生 Auth' }));
    await user.type(screen.getByLabelText('模型 ID'), 'claude-test-model');
    await user.click(screen.getByRole('button', { name: '创建 Agent' }));
    expect(current.calls.filter(call => call.method === 'agent.create')).toHaveLength(1);
  });

  it('does not invite a repeat submit after an unknown create outcome', async () => {
    const create = vi.fn(async () => ({
      kind: 'unknown' as const,
      message: '请求已发出但结果未知；不会自动重试，请核对列表确认实际状态。',
    }));
    render(<Page gateway={{ create, update: vi.fn(), delete: vi.fn() }} panel={{ kind: 'create' }} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('名称'), 'new-worker');
    await user.type(screen.getByLabelText('职责'), 'work');
    const submit = screen.getByRole('button', { name: '创建 Agent' });
    await user.click(submit);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('a failed detail read is an error, not proof the agent does not exist', async () => {
    const { client } = agentsTestClient(async () => { throw new Error('directory failed'); });
    render(<Page api={createAgentsApi(client)} panel={{ kind: 'detail', agentId: 'squire' }} />);
    expect(await screen.findByText('Agent 列表读取失败')).toBeInTheDocument();
    expect(screen.queryByText('找不到该 Agent')).not.toBeInTheDocument();
  });
  it('hides the previous identity detail while a replacement client is loading', async () => {
    const first = testApi();
    let release!: () => void;
    const hold = new Promise<void>(resolve => { release = resolve; });
    const second = agentsTestClient(async () => { await hold; return { ok: true, agents: [], agentsWithoutRoutine: [] }; });
    const nextApi = createAgentsApi(second.client);
    const view = render(<Page api={first.api} panel={{ kind: 'detail', agentId: 'squire' }} />);
    expect(await screen.findByTestId('agent-detail-panel')).toBeInTheDocument();
    view.rerender(<Page api={nextApi} panel={{ kind: 'detail', agentId: 'squire' }} />);
    expect(screen.queryByTestId('agent-detail-panel')).not.toBeInTheDocument();
    await act(async () => { release(); });
    expect(await screen.findByText('找不到该 Agent')).toBeInTheDocument();
  });

});
