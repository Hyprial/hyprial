import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useReducer } from 'react';
import { App } from '../src/App';
import { AppShell } from '../src/app/AppShell';
import { Rail } from '../src/components/Rail';
import { createFixtureState } from '../src/dev/fixtures/fixture-state';
import type { GuiPage } from '../src/state/gui-view-model';
import { appReducer, createEmptyState } from '../src/state/appReducer';
import { createLocalAgentGateway } from '../src/features/agents';
import { DEMO_BASELINE, authenticatedTestActions, demoFixtureCapabilities, demoFixtureState } from './support/demo-baseline';

const AGENT_SOURCE = { agents: [], connections: [], organizations: [] };

function renderApp(): void {
  render(
    <App
      initialState={demoFixtureState()}
      agentSeed={AGENT_SOURCE}
      agentGateway={createLocalAgentGateway({ agents: [], connections: [], organizations: [] })}
      capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch)}
    />,
  );
}

describe('fixture app shell', () => {
  it('does not open a chat just because the composition added an Agent after empty init', async () => {
    const user = userEvent.setup();
    const initialState = { ...createEmptyState('complete'), chatSendMode: 'reply' as const, chatReplyText: '隔离回复' };
    const target = { ...demoFixtureState().targets[0], id: 'agent:preview@example.invalid:local:new', displayName: '新 Agent', availability: 'available' as const };
    const { rerender } = render(<App capabilities={{ firstRun: authenticatedTestActions(), firstRunWizardEnabled: false }} initialState={initialState} stateSeed={{ targets: [] }} />);
    rerender(<App capabilities={{ firstRun: authenticatedTestActions(), firstRunWizardEnabled: false }} initialState={initialState} stateSeed={{ targets: [target] }} />);
    await user.click(screen.getByRole('button', { name: '聊天' }));
    expect(screen.queryByRole('button', { name: /新 Agent/ })).toBeNull();
    expect(screen.queryByRole('textbox', { name: '给 新 Agent 的消息' })).toBeNull();
  });
  it('renders the start page without development boundary labels', () => {
    renderApp();

    expect(
      screen.getByRole('heading', { level: 1, name: '开始' }),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/演示|样例|fixture|占位服务/i);
    expect(screen.queryByRole('button', { name: '任务' })).toBeNull();
  });

  it('does not expose launcher or backend diagnostics in the product shell', () => {
    renderApp();

    expect(screen.queryByTestId('stub-endpoint')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/127\.0\.0\.1|not-wired|HTTP 501/);
  });

  it('matches the shared prototype navigation groups', () => {
    renderApp();

    expect(
      screen.getByRole('navigation', { name: '主导航' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('group', { name: '工作' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('group', { name: '查找' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '聊天' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Agent' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    expect(screen.queryByRole('button', { name: '用户设置' })).not.toBeInTheDocument();
  });

  it('renders only the start-page status rows that have a source', () => {
    renderApp();

    expect(screen.queryByText('Agent 状态')).not.toBeInTheDocument();
    expect(screen.queryByText('任务状态')).toBeNull();
    expect(document.querySelector('[data-summary="tasks"]')).toBeNull();
    expect(screen.queryByText('平台绑定')).not.toBeInTheDocument();
    expect(screen.queryAllByText('未确认')).toHaveLength(0);
    expect(screen.queryByText('进行中任务')).not.toBeInTheDocument();
    expect(screen.queryByText('待你决定')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: '当前看板' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId('recent-conversation')).not.toBeInTheDocument();
  });

  it('renders no empty status card on a start page that has no source and no link', async () => {
    render(<App capabilities={{ firstRun: authenticatedTestActions(), firstRunWizardEnabled: false }}
      initialState={createEmptyState('complete')} />);
    await waitFor(() => expect(document.querySelector('[data-start-block="status"]')).toBeNull());
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('.start-status__list')).toBeNull();
  });

  it('renders no empty status card on a signed-out start page that has no source and no link', async () => {
    const firstRun = { getAccountState: async () => ({ account: { status: 'unauthenticated' as const }, device: { status: 'not-ready' as const } }) };
    render(<App capabilities={{ firstRun, firstRunWizardEnabled: false }} initialState={createEmptyState('complete')} />);
    await screen.findByRole('heading', { level: 1, name: '开始' });
    await waitFor(() => expect(document.querySelector('[data-start-block="status"]')).toBeNull());
    expect(document.querySelector('.start-status__list')).toBeNull();
  });

  it('hides the 设备 destination while no device port exists, signed out or signed in', async () => {
    const read = (status: 'unauthenticated' | 'authenticated') => ({ getAccountState: async () => ({
      account: status === 'authenticated' ? { status, owner: 'test@example.invalid' } : { status },
      device: { status: 'not-ready' as const },
    }) });
    for (const status of ['unauthenticated', 'authenticated'] as const) {
      const { unmount } = render(<App capabilities={{ firstRun: read(status), firstRunWizardEnabled: false }}
        initialPage="device" initialState={createEmptyState('complete')} />);
      await screen.findByRole('heading', { level: 1, name: '开始' });
      expect(screen.queryByRole('button', { name: '设备' })).toBeNull();
      expect(screen.queryByRole('button', { name: '初始化当前设备' })).toBeNull();
      unmount();
    }
  });

  it('lands the first-run route on Start when the wizard lacks a step port', async () => {
    const ports = { authenticateAccount: async () => ({ status: 'failed' as const, message: 'x' }), initializeDevice: async () => ({ status: 'failed' as const, message: 'x' }),
      saveModelKey: async () => ({ status: 'failed' as const, message: 'x' }) };
    for (const missing of [undefined, 'authenticateAccount', 'initializeDevice', 'saveModelKey'] as const) {
      const firstRun = missing === undefined ? {} : Object.fromEntries(Object.entries(ports).filter(([name]) => name !== missing));
      const { unmount } = render(<App initialPage="first-run" capabilities={{ firstRun }} />);
      await screen.findByRole('heading', { level: 1, name: '开始' });
      expect(screen.queryByRole('navigation', { name: '首次设置步骤' })).toBeNull();
      unmount();
    }
  });

  it('offers the first-run wizard when every step port exists', async () => {
    const done = async () => ({ status: 'failed' as const, message: 'x' });
    render(<App initialPage="first-run" capabilities={{ firstRun: {
      authenticateAccount: done, initializeDevice: done, saveModelKey: done,
    } }} />);
    expect(await screen.findByRole('navigation', { name: '首次设置步骤' })).toBeInTheDocument();
  });

  it('navigates between primary pages and marks the current navigation item', async () => {
    const user = userEvent.setup();
    renderApp();

    const chatNav = screen.getByRole('button', { name: '聊天' });
    await user.click(chatNav);

    expect(chatNav).toHaveAttribute('aria-current', 'page');
    expect(
      screen.getByRole('heading', { level: 1, name: '聊天' }),
    ).toBeInTheDocument();

    const agentsNav = screen.getByRole('button', { name: 'Agent' });
    await user.click(agentsNav);

    expect(agentsNav).toHaveAttribute('aria-current', 'page');
    expect(chatNav).not.toHaveAttribute('aria-current');
    expect(
      screen.getByRole('heading', { level: 1, name: 'Agent' }),
    ).toBeInTheDocument();
  });

  it('has no task entry and renders the start page for a stale task route', () => {
    renderApp();
    expect(screen.queryByRole('button', { name: '任务' })).toBeNull();
    expect(document.querySelector('[data-nav="tasks"]')).toBeNull();
  });

  it('renders the start page for the organization route when the composition has no org source', () => {
    render(
      <App
        initialState={{ ...demoFixtureState(), page: 'org' }}
        capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch)}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('[data-page="org"]')).toBeNull();
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  });

  it('opens the Agent create form from the start page', async () => {
    const user = userEvent.setup();
    renderApp();

    await user.click(screen.getByRole('button', { name: '创建 Agent' }));

    expect(
      screen.getByRole('heading', { level: 2, name: '创建 Agent' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('名称')).toBeInTheDocument();
  });

  it('opens account settings and shows no empty device entry without collecting account tokens', async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
    expect(screen.getByRole('region', { name: '账号登录' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: '当前设备' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '设备' })).toBeNull();
    expect(screen.queryByLabelText('访问令牌')).not.toBeInTheDocument();
  });
});


function navOrder(): string[] {
  return Array.from(document.querySelectorAll('nav[aria-label="主导航"] button[data-nav]'))
    .map(item => item.getAttribute('data-nav') ?? '');
}

function RailHarness({ hidden, page = 'start' }: { readonly hidden?: ReadonlySet<GuiPage>; readonly page?: GuiPage }) {
  const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
  return (
    <Rail
      page={page}
      accountMenuOpen={false}
      dispatch={dispatch}
      hiddenPages={hidden}
      notifications={{ open: false, unread: 0, items: state.centerNotices }}
    />
  );
}

describe('navigation gating', () => {
  it('renders the same three destination groups when nothing is hidden', () => {
    const { container, unmount } = render(<RailHarness />);
    const withoutProp = container.innerHTML;
    expect(navOrder()).toEqual(['start', 'chat', 'agents', 'org', 'device']);
    expect(document.querySelectorAll('.nav-separator')).toHaveLength(2);
    unmount();
    const empty = render(<RailHarness hidden={new Set()} />);
    expect(empty.container.innerHTML).toBe(withoutProp);
  });

  it('removes a hidden page from the rail and keeps the focus order of the others', async () => {
    const user = userEvent.setup();
    render(<RailHarness hidden={new Set<GuiPage>(['contacts'])} />);
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull();
    expect(navOrder()).toEqual(['start', 'chat', 'org', 'device']);
    await user.tab();
    const focused: string[] = [];
    for (let step = 0; step < 4; step += 1) {
      focused.push(document.activeElement?.getAttribute('data-nav') ?? '');
      await user.tab();
    }
    expect(focused).toEqual(['start', 'chat', 'org', 'device']);
  });

  it('does not render a group without a visible item or its separator', () => {
    render(<RailHarness hidden={new Set<GuiPage>(['chat'])} />);
    expect(screen.queryByRole('group', { name: '工作' })).toBeNull();
    expect(screen.getByRole('group', { name: '查找' })).toBeInTheDocument();
    expect(navOrder()).toEqual(['start', 'agents', 'org', 'device']);
    expect(document.querySelectorAll('.nav-separator')).toHaveLength(1);
  });

  it('keeps a leading separator out of the rail when the first group is hidden', () => {
    render(<RailHarness hidden={new Set<GuiPage>(['start'])} />);
    expect(navOrder()).toEqual(['chat', 'agents', 'org', 'device']);
    expect(document.querySelectorAll('.nav-separator')).toHaveLength(1);
    expect(document.querySelector('nav .nav-group:first-child .nav-separator')).toBeNull();
  });

  function ShellWith({ page, hidden, agentSource = true }: {
    readonly page: GuiPage;
    readonly hidden?: ReadonlySet<GuiPage>;
    readonly agentSource?: boolean;
  }) {
    const [state, dispatch] = useReducer(appReducer, undefined, () => ({
      ...createFixtureState(DEMO_BASELINE),
      page,
    }));
    return <AppShell state={state} dispatch={dispatch} firstRunWizardEnabled={false}
      hiddenPages={hidden} agentSeed={agentSource ? AGENT_SOURCE : undefined} />;
  }

  it('renders the start page for a hidden route and marks start as current', () => {
    render(<ShellWith page="contacts" hidden={new Set<GuiPage>(['contacts'])} />);
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull();
    expect(screen.getByRole('button', { name: '开始' })).toHaveAttribute('aria-current', 'page');
    expect(document.querySelector('[data-nav="agents"]')).toBeNull();
  });

  it('refuses the first-run route when the composition has no first-run ports', () => {
    render(<AppShell state={{ ...createFixtureState(DEMO_BASELINE), page: 'first-run' }} dispatch={() => undefined} />);
    expect(document.querySelector('[data-page="first-run"]')).toBeNull();
    expect(document.querySelector('[data-step]')).toBeNull();
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });

  it('never hides the start page', () => {
    render(<ShellWith page="start" hidden={new Set<GuiPage>(['start'])} />);
    expect(screen.getByRole('button', { name: '开始' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });

  it('renders the start page for a route outside the page type', async () => {
    render(<App capabilities={{ firstRun: authenticatedTestActions(), firstRunWizardEnabled: false }}
      initialState={{ ...createEmptyState('complete'), page: 'tasks' as never }} />);
    await waitFor(() => expect(document.querySelector('[data-page="start"]')).not.toBeNull());
    expect(document.querySelector('[data-page="tasks"]')).toBeNull();
  });

  it('keeps every page reachable when nothing is hidden', () => {
    render(<ShellWith page="contacts" />);
    expect(screen.getByRole('button', { name: 'Agent' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('heading', { level: 1, name: '开始' })).toBeNull();
  });
});
