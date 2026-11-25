import { authenticatedTestActions } from './support/demo-baseline';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dispatch } from 'react';
import { App } from '../src/App';
import { AppShell } from '../src/app/AppShell';
import { type FirstRunActions } from '../src/features/onboarding';
import { createFixtureFirstRunActions } from '../src/dev/first-run-capabilities';
import type { GuiCapabilities } from '../src/app/capabilities';
import {
  FIXTURE_AGENTS,
  FIXTURE_CONNECTIONS,
  FIXTURE_THREADS,
} from '../src/dev/fixtures/demo';
import type { StateBaseline } from '../src/state/appReducer';
import { createFixtureState } from '../src/dev/fixtures/fixture-state';
import type { AppAction, StateSeed } from '../src/state/types';
import type { SessionAgent as ChatTarget } from '../src/state/gui-view-model';
import { stubFirstRunActions } from './support/first-run-actions';

const DEMO_BASELINE: StateBaseline = {
  targets: FIXTURE_AGENTS,
  connections: FIXTURE_CONNECTIONS,
  threads: FIXTURE_THREADS,
};

function capabilitiesWith(
  wizardEnabled: boolean,
  firstRun: FirstRunActions = stubFirstRunActions(),
): GuiCapabilities {
  return { firstRun, firstRunWizardEnabled: wizardEnabled };
}

const DEVICE_PORT = stubFirstRunActions({ initializeDevice: async () => ({ status: 'failed', message: '未完成' }) });

function fixtureComposition(wizardEnabled: boolean, firstRun?: FirstRunActions) {
  return (
    <App
      baseline={DEMO_BASELINE}
      firstRunMode="complete"
      capabilities={capabilitiesWith(wizardEnabled, firstRun)}
    />
  );
}

function plainComposition(wizardEnabled: boolean, firstRun?: FirstRunActions) {
  return <App capabilities={capabilitiesWith(wizardEnabled, firstRun)} />;
}

async function openSettings(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
}

afterEach(cleanup);

describe('first-run gating is decided by injected capabilities', () => {
  it('renders the same first-run page in the fixture composition when enabled', () => {
    render(
      <App
        initialState={{ ...createFixtureState(DEMO_BASELINE, 'complete'), page: 'first-run' }}
        capabilities={capabilitiesWith(true)}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });

  it('renders the same first-run page in the plain composition when enabled', () => {
    render(
      <App
        initialState={{ ...createFixtureState('complete'), page: 'first-run' }}
        capabilities={capabilitiesWith(true)}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });

  it('keeps the converged settings free of a first-run entry in the fixture composition', async () => {
    const user = userEvent.setup();
    render(fixtureComposition(false, DEVICE_PORT));

    await openSettings(user);

    expect(screen.queryByRole('button', { name: '打开首次设置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '当前设备' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '设备' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  });

  it('keeps the converged settings free of a first-run entry in the plain composition', async () => {
    const user = userEvent.setup();
    render(plainComposition(false, DEVICE_PORT));

    await openSettings(user);

    expect(screen.queryByRole('button', { name: '打开首次设置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '当前设备' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '设备' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
  });

  it('refuses the first-run route in the shell when disabled and serves the start page', () => {
    render(
      <AppShell
        state={{ ...createFixtureState(DEMO_BASELINE), page: 'first-run' }}
        dispatch={() => undefined}
        firstRunWizardEnabled={false}
      />,
    );

    expect(screen.queryByRole('heading', { level: 1, name: '首次设置' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });

  it('serves the same first-run page in the shell when enabled', () => {
    render(
      <AppShell
        state={{ ...createFixtureState(DEMO_BASELINE), page: 'first-run' }}
        dispatch={() => undefined}
        firstRunWizardEnabled={true}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });
});

describe('first-run auto display follows composition inputs and first-run state', () => {
  it('auto-displays the wizard for an incomplete fixture first-run journey', () => {
    render(
      <App
        baseline={DEMO_BASELINE}
        firstRunMode="simulate"
        capabilities={capabilitiesWith(true)}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });

  it('does not auto-display the wizard when the fixture first-run journey is complete', () => {
    render(fixtureComposition(true));

    expect(screen.queryByRole('heading', { level: 1, name: '首次设置' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });

  it('never auto-displays the wizard in the plain composition', () => {
    render(plainComposition(true));

    expect(screen.queryByRole('heading', { level: 1, name: '首次设置' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });
});

describe('same inputs render the same product tree', () => {
  it('renders identical markup for identical composition props across mounts', () => {
    const props = {
      mockBaseline: DEMO_BASELINE,
      firstRunMode: 'complete' as const,
    };
    const first = render(<App {...props} />).container.innerHTML;
    cleanup();
    const second = render(<App {...props} />).container.innerHTML;

    expect(second).toBe(first);
  });

  it('renders the same shell for the same injected capabilities across mounts', () => {
    const props = {
      capabilities: capabilitiesWith(true),
    };
    const first = render(<App {...props} />).container.innerHTML;
    cleanup();
    const second = render(<App {...props} />).container.innerHTML;

    expect(second).toBe(first);
  });

  it('keeps development tooling out of the product tree', () => {
    const { container } = render(fixtureComposition(true));

    expect(container.querySelector('.dev-preview-frame')).toBeNull();
    expect(container.querySelector('.dev-preview-toolbar')).toBeNull();
    expect(container.querySelector('[data-dev-label]')).toBeNull();
    expect(document.body).not.toHaveTextContent(/演示数据|隔离预览/);
  });
});

describe('composition-owned state seed overlays the reducer state', () => {
  it('projects seeded targets without touching the reducer authority', async () => {
    const user = userEvent.setup();
    const seededTarget: ChatTarget = {
      id: 'agent-seeded',
      kind: 'agent',
      displayName: '注入目标',
      role: '组合根种子',
      availability: 'available',
      avatarTone: 'lilac',
      modelLabel: '种子模型',
      capabilities: [],
    };
    const stateSeed: StateSeed = { targets: [seededTarget] };
    render(
      <App
        baseline={DEMO_BASELINE}
        firstRunMode="complete"
        capabilities={capabilitiesWith(true, authenticatedTestActions())}
        stateSeed={stateSeed}
      />,
    );

    await user.click(screen.getByRole('button', { name: '聊天' }));

    expect(screen.getByRole('button', { name: /注入目标/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Squire/ })).not.toBeInTheDocument();
  });

  it('routes first-run actions to the composition owner instead of the reducer', async () => {
    const user = userEvent.setup();
    const routed: AppAction[] = [];
    render(
      <App
        baseline={DEMO_BASELINE}
        firstRunMode="simulate"
        capabilitiesFactory={(dispatch: Dispatch<AppAction>) => capabilitiesWith(true, createFixtureFirstRunActions('simulate', dispatch))}
        onFirstRunAction={(action) => routed.push(action)}
      />,
    );

    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await vi.waitFor(() => {
      expect(screen.getByRole('button', { name: '初始化当前设备' })).toBeEnabled();
    }, { timeout: 3_000 });
    await user.click(screen.getByRole('button', { name: '初始化当前设备' }));
    await vi.waitFor(() => {
      expect(routed.some((action) => action.type === 'first-run-succeed')).toBe(true);
    }, { timeout: 3_000 });
    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });

  it('feeds fixture capabilities from the app dispatch when no owner is present', async () => {
    const user = userEvent.setup();
    render(
      <App
        baseline={DEMO_BASELINE}
        firstRunMode="simulate"
        capabilitiesFactory={(dispatch: Dispatch<AppAction>) => capabilitiesWith(true, createFixtureFirstRunActions('simulate', dispatch))}
      />,
    );

    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await vi.waitFor(() => {
      expect(screen.getByRole('button', { name: /初始化|继续/ })).toBeEnabled();
    }, { timeout: 3_000 });

    expect(screen.getByRole('heading', { level: 1, name: '首次设置' })).toBeInTheDocument();
  });
});
