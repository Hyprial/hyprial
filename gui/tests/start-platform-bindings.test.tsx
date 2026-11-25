import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createAgentsApi } from '../src/features/agents';
import { createGeneratedClient } from '../src/client/generated';
import { agentStatusCounts } from '../src/pages/start/start-summary';
import { GuiCapabilitiesProvider } from '../src/app/capabilities';
import {
  type PlatformIntegration,
  type PlatformIntegrationsApi,
} from '../src/features/connections/platform-integrations-api';
import { StartPage } from '../src/pages/StartPage';
import { createEmptyState } from '../src/state/appReducer';
import { stubFirstRunActions } from './support/first-run-actions';

function integration(overrides: Partial<PlatformIntegration>): PlatformIntegration {
  return {
    name: 'feishu-main',
    provider: 'lark',
    status: 'online',
    configured: true,
    desired: true,
    processRunning: true,
    online: true,
    boundAgent: null,
    authorization: {
      adapter: 'feishu-main',
      appId: 'cli_test',
      state: 'authorized',
      capabilities: ['im:message'],
      missingCapabilities: [],
      checkedAt: 1,
    },
    identities: { state: 'synced', count: 1 },
    ...overrides,
  };
}

function api(rows: () => Promise<readonly PlatformIntegration[]>): PlatformIntegrationsApi {
  return {
    capabilities: {
      add: { available: false },
      authorization: { available: false },
      identities: { available: false },
    },
    list: rows,
    async start() {},
    async stop() {},
    async reload() {},
  };
}

function renderStart(platformIntegrations: PlatformIntegrationsApi) {
  render(
    <GuiCapabilitiesProvider value={{ firstRun: stubFirstRunActions(), platformIntegrations }}>
      <StartPage state={createEmptyState()} dispatch={vi.fn()} />
    </GuiCapabilitiesProvider>,
  );
  return document.querySelector('[data-summary="platform-bindings"]') as HTMLElement;
}

describe('start page 平台绑定 card', () => {
  it('counts observed running states and keeps incomplete or inferred observations unknown', async () => {
    const states = ['idle', 'blocked', 'not-running', 'suspected-stuck'];
    const rows = states.map(state => ({ actor: state, uri: `agent:owner:node:${state}`,
      runState: { state, certainty: 'observed', observationHealth: 'current' } }));
    const invoke = vi.fn().mockResolvedValue({ ok: true, agents: rows });
    const agents = createAgentsApi(createGeneratedClient({ invoke }));
    expect(agentStatusCounts(await agents.list())).toEqual({ normal: 1, abnormal: 3 });
    for (const runState of [
      { state: 'idle', certainty: 'inferred', observationHealth: 'current' },
      { state: 'idle', certainty: 'observed', observationHealth: 'stale' },
      { state: 'starting', certainty: 'observed', observationHealth: 'current' },
    ]) {
      invoke.mockResolvedValue({ ok: true, agents: [{ ...rows[0], runState }] });
      expect(agentStatusCounts(await agents.list())).toEqual({ normal: null, abnormal: null });
    }
  });

  it('does not read agent data before login or turn failed reads into zero', async () => {
    const invoke = vi.fn().mockRejectedValue(new Error('unavailable'));
    const agents = createAgentsApi(createGeneratedClient({ invoke }));
    const props = { state: createEmptyState(), dispatch: vi.fn() };
    const capabilities = { firstRun: stubFirstRunActions(), agents };
    const view = render(<GuiCapabilitiesProvider value={capabilities}>
      <StartPage {...props} onLoginRequired={vi.fn()} />
    </GuiCapabilitiesProvider>);
    expect(invoke).not.toHaveBeenCalled();
    view.rerender(<GuiCapabilitiesProvider value={capabilities}><StartPage {...props} /></GuiCapabilitiesProvider>);
    await waitFor(() => expect(invoke).toHaveBeenCalledTimes(1));
    expect(document.querySelector('[data-summary="agents"]')).toHaveTextContent('未确认');
  });

  it('reads real agent counts, and opens Agent settings', async () => {
    const invoke = vi.fn().mockResolvedValue({ ok: true, agents: [] });
    const agents = createAgentsApi(createGeneratedClient({ invoke }));
    const dispatch = vi.fn();
    render(<GuiCapabilitiesProvider value={{ firstRun: stubFirstRunActions(), agents }}>
      <StartPage state={createEmptyState()} dispatch={dispatch} />
    </GuiCapabilitiesProvider>);
    await waitFor(() => expect(document.querySelector('[data-summary="agents"]')).toHaveTextContent('0 / 0'));
    expect(document.querySelector('[data-summary="tasks"]')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看 Agent 设置' }));
    expect(dispatch).toHaveBeenCalledWith({ type: 'navigate', page: 'connections' });
  });

  it('counts only pinned platform bindings and separates forwarding from unavailable', async () => {
    const card = renderStart(api(async () => [
      integration({ name: 'a', boundAgent: 'agent:alice:node:worker' }),
      integration({ name: 'b', boundAgent: 'agent:bob:node:worker', online: false, processRunning: false }),
      integration({ name: 'c', boundAgent: 'agent:carol:node:worker', error: 'worker exited' }),
      integration({ name: 'd' }),
    ]));

    await waitFor(() => expect(card).toHaveTextContent('正常'));
    expect(card.querySelector('.start-status__value')).toHaveTextContent('1 / 2');
    expect(within(card).queryByText('未确认')).not.toBeInTheDocument();
  });

  it('reports zero when no adapter is pinned', async () => {
    const card = renderStart(api(async () => [integration({ name: 'a' })]));

    await waitFor(() => expect(card).toHaveTextContent('正常'));
    expect(card.querySelector('.start-status__value')).toHaveTextContent('0 / 0');
  });

  it('stays unconfirmed when the adapter list cannot be read', async () => {
    const card = renderStart(api(async () => { throw new Error('daemon unavailable'); }));

    await waitFor(() => expect(card).toHaveTextContent('未确认'));
    expect(card.querySelector('.start-status-grid')).toBeNull();
  });
});
