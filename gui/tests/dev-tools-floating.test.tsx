import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  DevPreviewApp,
  getDevPreviewStore,
  resetDevPreviewStore,
  type DevScenarioState,
} from '../src/dev';

const css = readFileSync(
  resolve(process.cwd(), 'src/dev/dev-module-index.css'),
  'utf8',
);

function rule(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 's'));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? '';
}

async function openTools(
  user: ReturnType<typeof userEvent.setup>,
): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: '开发工具' }));
  return screen.getByRole('dialog', { name: '开发工具面板' });
}

function mountWithRoot(): HTMLElement {
  const root = document.createElement('div');
  root.id = 'root';
  document.body.appendChild(root);
  return root;
}

beforeEach(() => {
  window.history.replaceState(null, '', '/');
  window.location.hash = '';
  resetDevPreviewStore();
});

afterEach(() => {
  window.location.hash = '';
  document.getElementById('root')?.remove();
  document.querySelectorAll('.dev-tools-host').forEach((node) => node.remove());
});

describe('floating dev tools layer', () => {
  it('opens archived scenarios separately without replacing the active journey', async () => {
    const user = userEvent.setup({ delay: null });
    const store = getDevPreviewStore();
    store.setScenario({ j1: 'logged-in-init', j2: 'm05' });
    const scenario = store.getScenario();
    const session = store.getSession();
    const originalUrl = window.location.href;
    render(<DevPreviewApp />);
    const panel = await openTools(user);

    await user.click(within(panel).getByRole('tab', { name: '归档场景' }));
    await user.selectOptions(within(panel).getByLabelText('J2 场景'), 'm04');
    await user.selectOptions(within(panel).getByLabelText('J3 场景'), 'm23');
    await user.selectOptions(within(panel).getByLabelText('组织数据'), 'owner');
    const link = within(panel).getByRole('link', { name: '打开归档场景 ↗' });
    const url = new URL(link.getAttribute('href')!, window.location.href);

    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(url.searchParams.get('hyprialScenario')).toBe('j1=account-ready;j2=m04;j3=m23;orgData=owner');
    expect(url.searchParams.get('hyprialFirstRun')).toBe('true');
    expect(store.getScenario()).toBe(scenario);
    expect(store.getSession()).toBe(session);
    expect(window.location.href).toBe(originalUrl);

    await user.click(within(panel).getByRole('tab', { name: '当前旅程' }));
    expect(within(panel).queryByLabelText('J1 场景')).not.toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '已登录 · init' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('supports keyboard tabs and retains the existing panel close behavior', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);
    const current = within(panel).getByRole('tab', { name: '当前旅程' });
    current.focus();
    await user.keyboard('{ArrowRight}');
    expect(within(panel).getByRole('tab', { name: '归档场景' })).toHaveFocus();
    expect(within(panel).getByRole('tabpanel', { name: '归档场景' })).toBeVisible();
    await user.keyboard('{Home}');
    expect(current).toHaveFocus();
    expect(current).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开发工具' })).toHaveFocus();
  });

  it('recognizes an archived scenario without marking the current empty journey active', async () => {
    const user = userEvent.setup({ delay: null });
    getDevPreviewStore().setScenario({ j1: 'account-ready', j2: 'm04', j3: 'm23', orgData: 'owner' });
    render(<DevPreviewApp />);
    const panel = await openTools(user);
    expect(within(panel).getByRole('tab', { name: '归档场景' })).toHaveAttribute('aria-selected', 'true');
    expect(within(panel).getByLabelText('J2 场景')).toHaveValue('m04');
    expect(within(panel).getByLabelText('J3 场景')).toHaveValue('m23');
    await user.click(within(panel).getByRole('tab', { name: '当前旅程' }));
    expect(panel.querySelector('[data-dev-scenario-active="true"]')).toBeNull();
  });

  it('renders only the collapsed floating ball outside #root by default', () => {
    const root = mountWithRoot();
    render(<DevPreviewApp />);

    const ball = screen.getByRole('button', { name: '开发工具' });
    expect(ball).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('J1 场景')).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '开发模块索引' })).not.toBeInTheDocument();
    expect(root.contains(ball)).toBe(false);
    expect(ball.closest('.dev-tools-layer')).not.toBeNull();
    expect(screen.queryByText(/Fixture/)).not.toBeInTheDocument();
  });

  it('expands a non-modal panel with source notice and the two auth/init states', async () => {
    const root = mountWithRoot();
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);

    const ball = screen.getByRole('button', { name: '开发工具' });
    expect(ball).toHaveAttribute('aria-haspopup', 'dialog');
    await user.click(ball);

    const panel = screen.getByRole('dialog', { name: '开发工具面板' });
    expect(ball).toHaveAttribute('aria-expanded', 'true');
    expect(panel).toHaveAttribute('aria-modal', 'false');
    expect(root).not.toHaveAttribute('aria-hidden');
    expect(within(panel).getByRole('note', { name: '开发预览数据来源' })).toBeInTheDocument();
    expect(within(panel).getByRole('group', { name: '账号与初始化状态' })).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '未登录 · init' })).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '已登录 · init' })).toBeInTheDocument();
    expect(within(panel).queryByLabelText('J1 场景')).not.toBeInTheDocument();
    expect(within(panel).queryByRole('list', { name: '开发模块索引' })).not.toBeInTheDocument();
  });

  it('moves focus into the panel when opened from the ball', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const ball = screen.getByRole('button', { name: '开发工具' });
    await user.click(ball);
    const panel = screen.getByRole('dialog', { name: '开发工具面板' });
    expect(panel).toHaveFocus();
  });

  it('closes on Escape and restores focus to the floating ball', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const ball = screen.getByRole('button', { name: '开发工具' });
    await user.click(ball);
    expect(screen.getByRole('dialog', { name: '开发工具面板' })).toHaveFocus();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();
    expect(ball).toHaveAttribute('aria-expanded', 'false');
    expect(ball).toHaveFocus();
  });

  it('closes on outside pointer down without stealing focus', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const ball = screen.getByRole('button', { name: '开发工具' });
    await user.click(ball);
    expect(screen.getByRole('dialog', { name: '开发工具面板' })).toBeInTheDocument();

    await user.click(document.body);

    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();
    expect(ball).not.toHaveFocus();
  });

  it('keeps the ball keyboard operable and toggles with Enter', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const ball = screen.getByRole('button', { name: '开发工具' });
    ball.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('dialog', { name: '开发工具面板' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();
    expect(ball).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('dialog', { name: '开发工具面板' })).toBeInTheDocument();
  });

  it('ignores pointer down inside the panel or on the ball when open', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const ball = screen.getByRole('button', { name: '开发工具' });
    await user.click(ball);
    const panel = screen.getByRole('dialog', { name: '开发工具面板' });

    await user.click(within(panel).getByRole('note', { name: '开发预览数据来源' }));

    expect(screen.getByRole('dialog', { name: '开发工具面板' })).toBeInTheDocument();
  });
});

describe('dev scenario store', () => {
  it('starts with the default scenario set and notifies subscribers on change', () => {
    const store = getDevPreviewStore();
    expect(store.getScenario()).toMatchObject({
      j1: 'logged-out-init',
      j2: 'm05',
      j3: 'baseline',
      j5: 'baseline',
      j6: 'baseline',
      j7: 'baseline',
      orgData: 'off',
    });

    const seen: DevScenarioState[] = [];
    store.subscribeScenario(() => seen.push(store.getScenario()));
    store.setScenario({ j1: 'logged-in-init' });
    store.setScenario({ j2: 'm04' });

    expect(seen).toHaveLength(2);
    expect(seen[0]?.j1).toBe('logged-in-init');
    expect(seen[1]?.j2).toBe('m04');
    expect(store.getScenario()).toMatchObject({ j1: 'logged-in-init', j2: 'm04' });
  });

  it('ignores legacy hash selectors and starts from the logged-out init baseline', () => {
    window.location.hash = 'j1=provider-empty&j3=contract-success&j10=add-failure&j4=unavailable';
    resetDevPreviewStore();

    expect(getDevPreviewStore().getScenario()).toMatchObject({
      j1: 'logged-out-init',
      j2: 'm05',
      j3: 'baseline',
      j5: 'baseline',
      j6: 'baseline',
      j7: 'baseline',
      orgData: 'off',
      j4: 'ready',
      j10: 'ready',
    });
    window.location.hash = '';
    resetDevPreviewStore();
    expect(getDevPreviewStore().getScenario()).toMatchObject({
      j1: 'logged-out-init',
      j3: 'baseline',
    });
  });

  it('switches between the two auth/init states and resets the linked session', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);
    const store = getDevPreviewStore();

    await user.click(within(panel).getByRole('button', { name: '已登录 · init' }));

    expect(store.getScenario()).toMatchObject({
      j1: 'logged-in-init',
      j2: 'm05',
      j3: 'baseline',
      orgData: 'off',
    });
    expect(store.getSession().firstRun.status).toBe('completed');

    await user.click(within(panel).getByRole('button', { name: '未登录 · init' }));
    expect(store.getScenario()).toMatchObject({ j1: 'logged-out-init', j2: 'm05' });
    expect(store.getSession().firstRun.status).toBe('in_progress');
  });
});

describe('parity hooks and URL mirror', () => {
  it('exposes the WP6 hooks on the layer, toggle and panel', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);

    const layer = document.querySelector('[data-dev-tools]');
    expect(layer).not.toBeNull();
    const ball = screen.getByRole('button', { name: '开发工具' });
    expect(ball).toHaveAttribute('data-dev-tools-toggle');
    expect(ball.closest('[data-dev-tools]')).toBe(layer);
    expect(layer?.contains(document.getElementById('root') ?? document.body)).toBe(false);

    await user.click(ball);
    const panel = screen.getByRole('dialog', { name: '开发工具面板' });
    expect(panel).toHaveAttribute('data-dev-tools-panel');
    expect(panel.closest('[data-dev-tools]')).toBe(layer);
  });

  it('offers only auth/init controls with active marking and URL mirror on switch', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);

    const controls = Array.from(
      panel.querySelectorAll<HTMLElement>('[data-dev-scenario-control]'),
    );
    expect(controls).toHaveLength(2);
    const values = controls.map((control) => control.getAttribute('data-dev-scenario-value'));
    expect(values).toContain('logged-out-init');
    expect(values).toContain('logged-in-init');
    expect(new Set(values).size).toBe(values.length);

    const target = controls.find((control) => control.getAttribute('data-dev-scenario-value') === 'logged-in-init');
    expect(target).toBeDefined();

    await user.click(target!);

    const mirrored = new URLSearchParams(window.location.search).get('hyprialScenario');
    expect(mirrored).toBe('logged-in-init');
    expect(getDevPreviewStore().getScenario()).toMatchObject({ j1: 'logged-in-init', j2: 'm05' });

    await act(async () => {
      getDevPreviewStore().setScenario({ j1: 'logged-out-init', j2: 'm05' });
    });
    const active = panel.querySelector('[data-dev-scenario-control][data-dev-scenario-active="true"]');
    expect(active?.getAttribute('data-dev-scenario-value')).toBe('logged-out-init');
  });

  it('leaves the URL untouched until a scenario switch happens', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    await openTools(user);

    expect(window.location.search).toBe('');
  });

  it('preserves unrelated URL parameters while mirroring', async () => {
    window.history.replaceState(null, '', '/?keep=1');
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);

    const target = panel.querySelector<HTMLElement>(
      '[data-dev-scenario-control][data-dev-scenario-value="logged-in-init"]',
    );
    await user.click(target!);

    const params = new URLSearchParams(window.location.search);
    expect(params.get('keep')).toBe('1');
    expect(params.get('hyprialScenario')).toBe('logged-in-init');
    window.history.replaceState(null, '', '/');
  });

  it('mirrors into hyprialScenario only and drops the legacy hyprialFixture alias', async () => {
    window.history.replaceState(null, '', '/?hyprialFixture=stale&keep=1');
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);

    const target = panel.querySelector<HTMLElement>(
      '[data-dev-scenario-control][data-dev-scenario-value="logged-in-init"]',
    );
    await user.click(target!);

    const params = new URLSearchParams(window.location.search);
    expect(params.get('hyprialScenario')).toBe('logged-in-init');
    expect(params.get('hyprialFixture')).toBeNull();
    expect(params.get('keep')).toBe('1');
    window.history.replaceState(null, '', '/');
  });

  it('mirrors the logged-in init alias back through the store defaults', async () => {
    const user = userEvent.setup({ delay: null });
    render(<DevPreviewApp />);
    const panel = await openTools(user);

    const complete = panel.querySelector<HTMLElement>(
      '[data-dev-scenario-control][data-dev-scenario-value="logged-in-init"]',
    );
    expect(complete).not.toBeNull();
    await user.click(complete!);

    expect(getDevPreviewStore().getScenario()).toMatchObject({ j1: 'logged-in-init', j2: 'm05' });
    expect(new URLSearchParams(window.location.search).get('hyprialScenario')).toBe(
      'logged-in-init',
    );
  });
});

describe('floating tools css contract', () => {
  it('anchors the layer bottom-right with safe-area insets outside the product flow', () => {
    expect(rule(css, '.dev-tools-layer')).toMatch(/position\s*:\s*fixed/);
    expect(css).toMatch(/env\(safe-area-inset-bottom/);
    expect(css).toMatch(/env\(safe-area-inset-right/);
    expect(css).not.toMatch(/\.dev-preview-frame/);
    expect(css).not.toMatch(/\.dev-preview-toolbar/);
    expect(css).not.toMatch(/\.dev-preview-footer/);
  });

  it('keeps the panel within viewport bounds with internal scrolling', () => {
    const panel = rule(css, '.dev-tools-panel');
    expect(panel).toMatch(/overflow-y\s*:\s*auto/);
    expect(panel).toMatch(/max-height/);
    expect(panel).toMatch(/width\s*:\s*min\(/);
  });

  it('adapts to 375 and 760 widths while 1440 stays on the base rules', () => {
    expect(css).toMatch(/@media\s*\(max-width:\s*760px\)/);
    expect(css).toMatch(/@media\s*\(max-width:\s*375px\)/);
    const narrow = css.slice(css.search(/@media\s*\(max-width:\s*375px\)/));
    expect(narrow).toMatch(/\.dev-tools-panel\s*\{/);
  });

  it('lifts the collapsed layer above the ≤640 bottom nav', () => {
    const narrow = css.slice(css.search(/@media\s*\(max-width:\s*640px\)/));
    const layerRule = rule(narrow, '.dev-tools-layer');
    expect(layerRule).toMatch(
      /bottom\s*:\s*calc\([^}]*56px[^}]*safe-area-inset-bottom[^}]*\)/,
    );
    expect(narrow).toMatch(/\.dev-tools-panel\s*\{[^}]*max-height/s);
  });

  it('never hides or moves the product bottom navigation to make room', () => {
    const narrow = css.slice(css.search(/@media\s*\(max-width:\s*640px\)/));
    expect(narrow).not.toMatch(/\.(?:nav|nav-item|rail|shell|content)\b[^{]*\{/);
    expect(narrow).not.toMatch(/display\s*:\s*none/);
  });
});
