import { CHAT_FIXTURE_TARGET } from '../src/dev/fixtures/chat-channels/directory';
import { createAgentChatMock } from '../src/dev/mock/agent-chat';
import { createDevSettingsApis } from '../src/dev/mock/settings';
import { createFixtureFirstRunActions } from '../src/dev/first-run-capabilities';
import { appReducer, type StateBaseline } from '../src/state/appReducer';
import { createFixtureState } from '../src/dev/fixtures/fixture-state';
import { FIXTURE_AGENTS, FIXTURE_CONNECTIONS, FIXTURE_THREADS } from '../src/dev/fixtures/demo';
import { buildChatSeed } from '../src/dev/chat-scenarios';
import { render, screen, within, waitFor } from '@testing-library/react';
import { App } from '../src/App';
import { demoFixtureCapabilities, demoFixtureState } from './support/demo-baseline';
import { expectUnavailableToast } from './support/notify-harness';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DevPreviewApp } from '../src/dev/DevPreviewApp';
import { resetDevPreviewStore, getDevPreviewStore } from '../src/dev';
import {
  DevAgentScenarioSelector,
  DevChatScenarioSelector,
  DevMachineScenarioSelector,
  DevOrgScenarioSelector,
  DevScenarioSelector,
  DevTopologyScenarioSelector,
} from '../src/dev/DevScenarioSelector';
import { scenarioFromId } from '../src/dev/parity-composition';

const DEMO_BASELINE: StateBaseline = {
  targets: FIXTURE_AGENTS,
  connections: FIXTURE_CONNECTIONS,
  threads: FIXTURE_THREADS,
};

beforeEach(() => {
  window.location.hash = '';
  resetDevPreviewStore();
});

afterEach(() => {
  window.location.hash = '';
  document.querySelectorAll('.dev-tools-host').forEach((node) => node.remove());
});

async function openPanel(
  user: ReturnType<typeof userEvent.setup>,
): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: '开发工具' }));
  return screen.getByRole('dialog', { name: '开发工具面板' });
}

describe('scenario selector surfaces', () => {
  it('accepts the logged-in and logged-out init ids through the parity URL codec', () => {
    const guards = {
      isJ2Scenario: (_value: string): _value is string => true,
      isJ5Scenario: (_value: string): _value is string => true,
      isJ6Scenario: (_value: string): _value is string => true,
      isJ7Scenario: (_value: string): _value is string => true,
      isOrgDevScenario: (_value: string): _value is string => true,
    };
    expect(scenarioFromId('logged-in-init', guards)).toEqual(scenarioFromId('j1=logged-in-init', guards));
    expect(scenarioFromId('logged-out-init', guards)).toEqual(scenarioFromId('j1=logged-out-init', guards));
    expect(scenarioFromId('j1=logged-in-init', guards).j1).toBe('logged-in-init');
    expect(scenarioFromId('j1=logged-out-init', guards).j1).toBe('logged-out-init');
  });

  it('exposes exactly the documented scenario options per selector', () => {
    const onChange = vi.fn();
    render(<DevScenarioSelector value="simulate" onChange={onChange} />);

    const j1 = screen.getByLabelText('J1 场景');
    expect(j1).toHaveAttribute('data-mock-scenario', 'j1');
    expect(
      within(j1)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual([
      'simulate',
      'account-failure',
      'model-failure',
      'conversation-failure',
      'model-unavailable',
      'complete',
      'provider-success',
      'provider-empty',
      'provider-error',
      'provider-unconfigured',
      'account-success',
      'account-empty',
      'account-error',
      'account-disabled',
      'account-ready',
      'logged-out-init',
      'logged-in-init',
    ]);
  });

  it('keeps the J2, J3, J5, J6 and J7 option sets and the data-mock-scenario hooks', () => {
    render(
      <>
        <DevAgentScenarioSelector value="baseline" onChange={vi.fn()} />
        <DevChatScenarioSelector value="baseline" onChange={vi.fn()} />
        <DevOrgScenarioSelector value="baseline" onChange={vi.fn()} />
        <DevMachineScenarioSelector value="baseline" onChange={vi.fn()} />
        <DevTopologyScenarioSelector value="baseline" onChange={vi.fn()} />
      </>,
    );

    const j2 = screen.getByLabelText('J2 场景');
    expect(j2).toHaveAttribute('data-mock-scenario', 'j2');
    expect(
      within(j2)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['baseline', 'm04', 'm05', 'm06']);

    const j3 = screen.getByLabelText('J3 场景');
    expect(j3).toHaveAttribute('data-mock-scenario', 'j3');
    expect(
      within(j3)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['baseline', 'm06', 'm07', 'm08', 'm23',
      'contract-success', 'contract-empty', 'contract-error', 'contract-unconfigured', 'contract-unknown', 'contract-failure']);

    const j5 = screen.getByLabelText('J5 场景');
    expect(j5).toHaveAttribute('data-mock-scenario', 'j5');
    expect(
      within(j5)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['baseline', 'm11', 'm12', 'm13', 'm25']);

    const j6 = screen.getByLabelText('J6 场景');
    expect(j6).toHaveAttribute('data-mock-scenario', 'j6');
    expect(
      within(j6)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['baseline', 'm14', 'm15']);

    const j7 = screen.getByLabelText('J7 场景');
    expect(j7).toHaveAttribute('data-mock-scenario', 'j7');
    expect(
      within(j7)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value')),
    ).toEqual(['baseline', 'm16', 'm17']);
  });

  it('keeps the floating panel limited to the two auth/init controls', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openPanel(user);

    expect(document.querySelectorAll('[data-mock-scenario]')).toHaveLength(0);
    expect(within(panel).queryAllByRole('combobox')).toHaveLength(0);
    expect(within(panel).getByRole('button', { name: '未登录 · init' })).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '已登录 · init' })).toBeInTheDocument();
  });

  it('forwards auth/init state changes to the shared store', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openPanel(user);

    await user.click(within(panel).getByRole('button', { name: '已登录 · init' }));

    expect(getDevPreviewStore().getScenario()).toMatchObject({
      j1: 'logged-in-init',
      j2: 'm05',
      j3: 'baseline',
      j5: 'baseline',
      orgData: 'off',
    });
  });
});

describe('J3 local history semantics', () => {
  it.each([['m06', 0], ['m07', 1], ['m08', 0], ['m23', 2]] as const)('%s explicitly owns its history', (scenario, count) => {
    const state = createFixtureState(DEMO_BASELINE, 'complete', null, buildChatSeed(scenario));
    expect(state.threads[CHAT_FIXTURE_TARGET]).toHaveLength(count);
    if (scenario === 'm07') expect(state.threads[CHAT_FIXTURE_TARGET][0]).toMatchObject({ author: 'user', delivery: 'failed', text: '请帮我检查模型连接。' });
    if (scenario === 'm06') expect(state.targets.find(t => t.id === CHAT_FIXTURE_TARGET)?.availability).toBe('needs-connection');
  });
});

it.each(['m06', 'm07', 'm08', 'm23'] as const)('%s uses the contract directory and canonical URI throughout local state', async scenario => {
  const directory = await createAgentChatMock('success').call('targets.list') as { targets: { targetUri: string; displayName: string }[] };
  let state = createFixtureState(DEMO_BASELINE, 'complete', null, buildChatSeed(scenario));
  expect(state.targets.map(t => ({ targetUri: t.id, displayName: t.displayName }))).toEqual(directory.targets.map(({ targetUri, displayName }) => ({ targetUri, displayName })));
  expect(state.activeTargetId).toBe(CHAT_FIXTURE_TARGET);
  expect(state.targets[0]).toMatchObject({ role: CHAT_FIXTURE_TARGET, capabilities: [], availability: scenario === 'm06' ? 'needs-connection' : 'available' });
  expect(Object.keys(state.threads)).toEqual(directory.targets.map(target => target.targetUri));
  expect(state.targets.map(target => target.availability)).toEqual(scenario === 'm06' ? Array(4).fill('needs-connection') : ['available', 'available', 'available', 'needs-connection']);
  if (scenario === 'm08') expect(Object.values(state.threads).every(thread => thread.length === 0)).toBe(true);
  expect(state.threads[state.targets[3].id]).toEqual([]);
  if (scenario === 'm23') expect(new Set(state.targets.slice(0, 3).map(target => state.threads[target.id][0].text)).size).toBe(3);
  expect(state.threads[CHAT_FIXTURE_TARGET].every(message => message.targetId === CHAT_FIXTURE_TARGET)).toBe(true);
  state = appReducer(state, { type: 'set-draft', value: '检查 URI' });
  expect(state.drafts).toEqual({ [CHAT_FIXTURE_TARGET]: '检查 URI' });
  if (scenario === 'm07') {
    state = appReducer(state, { type: 'chat-retry', targetId: CHAT_FIXTURE_TARGET, messageId: 'seed-failed-1' });
    expect(state.threads[CHAT_FIXTURE_TARGET][0]).toMatchObject({ targetId: CHAT_FIXTURE_TARGET, text: '检查 URI', delivery: 'pending' });
  }
  if (scenario === 'm08') {
    state = appReducer(state, { type: 'chat-send', targetId: CHAT_FIXTURE_TARGET, text: '检查 URI' });
    expect(state.threads[CHAT_FIXTURE_TARGET][0]).toMatchObject({ targetId: CHAT_FIXTURE_TARGET, text: '检查 URI', delivery: 'pending' });
  }
});

it('keeps the baseline directory unchanged and rejects an empty injected directory', () => {
  expect(createFixtureState(DEMO_BASELINE).targets).toEqual(FIXTURE_AGENTS);
  expect(createFixtureState(DEMO_BASELINE).activeTargetId).toBe('agent-squire');
  expect(() => createFixtureState(DEMO_BASELINE, 'complete', null, { targets: [] as never })).toThrow(/nonempty/i);
});

describe('settings convergence under fixture capabilities', () => {
  async function openSettingsViaMenu(
    user: ReturnType<typeof userEvent.setup>,
  ): Promise<void> {
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
  }

  it.each([
    ['success', 'preview@example.invalid'],
    ['empty', '未登录'],
    ['error', '模拟：账号授权失败。'],
  ])('drives the settings account states through the %s account fixture', async (scenario, expected) => {
    const user = userEvent.setup({ delay: null });
    render(<App initialState={demoFixtureState()} capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch, { accountLoginScenario: scenario as 'success' | 'empty' | 'error' })} />);
    await openSettingsViaMenu(user);
    expect((await screen.findAllByText(expected)).length).toBeGreaterThan(0);
  });

  it('keeps the settings login controls operable and notifies through the refused-login fixture', async () => {
    const user = userEvent.setup({ delay: null });
    render(<App initialState={demoFixtureState()} capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch, { accountLoginScenario: 'disabled' })} />);
    await openSettingsViaMenu(user);
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });
    expect(login).toBeEnabled();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();
    await user.click(login);
    expectUnavailableToast('登录暂不可用。');
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
  });

  it('initializes provider fixtures without injecting provider controls into settings', async () => {
    const user = userEvent.setup({ delay: null });
    render(<App initialState={demoFixtureState()} capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch, { agentProviderScenario: 'empty' })} />);
    await openSettingsViaMenu(user);
    expect(await screen.findByRole('heading', { name: '用户设置' })).toBeInTheDocument();
    expect(screen.queryByText('暂无 Agent')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '登录服务' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '当前设备' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '设备' })).toBeNull();
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  });

  it('projects the configured settings cards and supports local preview logout', async () => {
    const user = userEvent.setup({ delay: null });
    const settings = createDevSettingsApis();
    render(<App initialState={demoFixtureState()} capabilitiesFactory={(dispatch) => ({
      firstRun: createFixtureFirstRunActions('complete', dispatch, 'ready'),
      firstRunWizardEnabled: true,
      ...settings,
    })} />);
    await openSettingsViaMenu(user);
    expect(await screen.findByRole('heading', { name: '用户设置' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '用户与平台身份绑定' })).not.toBeInTheDocument();
    expect(screen.queryByText('预览访客')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^设备$/ }));
    expect(await screen.findByText('preview-daemon')).toBeInTheDocument();
    expect(screen.queryByText('preview-node')).not.toBeInTheDocument();
    await openSettingsViaMenu(user);
    expect(screen.getByRole('button', { name: '退出本机登录' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '退出本机登录' }));
    await user.click(screen.getByRole('button', { name: '确认退出' }));
    expect(await screen.findByText('本机退出已确认')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /^设备$/ }));
    await waitFor(() => expect(screen.queryByText('preview-daemon')).not.toBeInTheDocument());
    await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
    expect(await screen.findByText('请先登录')).toBeInTheDocument();
  });
});
