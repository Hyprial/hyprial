/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): the seam contract.
 * mountDevTools must mount the same floating layer over a real-backend-shaped
 * context and over the fixture context, stay outside the product #root, and
 * render no fixture-only tool and no explanatory text over the backend: no operable
 * scenario or session control, no URL mirror, no request, no storage write.
 */
import { act, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createFixtureDevToolsContext, resetDevPreviewStore } from '../../src/dev/fixture-tools';
import {
  DEV_TOOLS_CONTAINER_ID,
  ensureDevToolsContainer,
  mountDevTools,
  type DevToolsContext,
  type DevToolsHandle,
} from '../../src/dev/tools';

const BACKEND: DevToolsContext = { dataSource: 'backend', apiDocs: false };
const BACKEND_WITH_DOCS: DevToolsContext = { dataSource: 'backend', apiDocs: true };

let productRoot: HTMLElement;
let handles: DevToolsHandle[] = [];

async function mount(context: DevToolsContext): Promise<DevToolsHandle> {
  let handle: DevToolsHandle | undefined;
  await act(async () => {
    handle = mountDevTools(ensureDevToolsContainer(productRoot), context);
  });
  handles.push(handle!);
  return handle!;
}

async function openPanel(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: '开发工具' }));
  return screen.getByRole('dialog', { name: '开发工具面板' });
}

beforeEach(() => {
  window.history.replaceState(null, '', '/?keep=1');
  window.location.hash = '';
  resetDevPreviewStore();
  productRoot = document.createElement('div');
  productRoot.id = 'root';
  productRoot.innerHTML = '<main data-page="start">product</main>';
  document.body.appendChild(productRoot);
});

afterEach(async () => {
  await act(async () => {
    handles.forEach((handle) => handle.unmount());
  });
  handles = [];
  document.getElementById(DEV_TOOLS_CONTAINER_ID)?.remove();
  productRoot.remove();
  window.history.replaceState(null, '', '/');
  vi.unstubAllGlobals();
});

describe('mountDevTools over a real-backend-shaped context', () => {
  it('mounts the collapsed floating layer outside the product #root', async () => {
    await mount(BACKEND);

    const container = document.getElementById(DEV_TOOLS_CONTAINER_ID);
    expect(container).not.toBeNull();
    expect(productRoot.contains(container)).toBe(false);
    expect(productRoot.nextElementSibling).toBe(container);

    const ball = screen.getByRole('button', { name: '开发工具' });
    expect(ball).toHaveAttribute('aria-expanded', 'false');
    const layer = ball.closest('[data-dev-tools]');
    expect(layer).toHaveAttribute('data-dev-tools-source', 'backend');
    expect(productRoot.contains(layer)).toBe(false);
    expect(productRoot.innerHTML).toBe('<main data-page="start">product</main>');
  });

  it('renders only the module index over the real backend', async () => {
    const user = userEvent.setup({ delay: null });
    await mount(BACKEND);
    const panel = await openPanel(user);

    expect(within(panel).getByRole('list', { name: '开发模块索引' })).toBeInTheDocument();
    expect(Array.from(panel.children).map((child) => child.className)).toEqual(['dev-module-dock']);
    expect(within(panel).queryByRole('note')).toBeNull();
    for (const selector of [
      '[data-dev-tools-unavailable]',
      '[data-dev-tool]',
      '[data-dev-tool-state]',
      '[data-dev-tools-notice]',
      '[aria-disabled="true"]',
    ]) {
      expect(panel.querySelector(selector)).toBeNull();
    }
    expect(within(panel).queryByRole('list', { name: '当前不可用的开发工具' })).toBeNull();
    for (const text of ['真实后端', '模拟', '不可用']) {
      expect(panel.textContent).not.toContain(text);
    }
  });

  it('renders the API docs entry only when the capability says so', async () => {
    const user = userEvent.setup({ delay: null });
    const handle = await mount(BACKEND_WITH_DOCS);
    let panel = await openPanel(user);

    const entries = panel.querySelectorAll('[data-dev-tools-api-docs]');
    expect(entries).toHaveLength(1);
    const link = entries[0] as HTMLAnchorElement;
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toMatch(/(^|\/)docs$/);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'opener');
    expect(Array.from(panel.children).map((child) => child.className)).toEqual(['dev-tools-api-docs', 'dev-module-dock']);
    expect(within(panel).getByRole('list', { name: '开发模块索引' })).toBeInTheDocument();

    await act(async () => {
      handle.unmount();
    });
    await mount(BACKEND);
    panel = await openPanel(user);
    expect(panel.querySelector('[data-dev-tools-api-docs]')).toBeNull();
    expect(within(panel).queryAllByRole('link')).toHaveLength(0);
    expect(Array.from(panel.children).map((child) => child.className)).toEqual(['dev-module-dock']);
    expect(panel.textContent).toBe(panel.querySelector('.dev-module-dock')!.textContent);
  });

  it('renders no operable scenario or session control and performs no side effect', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const user = userEvent.setup({ delay: null });
    await mount(BACKEND);
    const panel = await openPanel(user);

    expect(within(panel).queryAllByRole('combobox')).toHaveLength(0);
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    expect(within(panel).queryAllByRole('tab')).toHaveLength(0);
    expect(within(panel).queryAllByRole('link')).toHaveLength(0);
    expect(panel.querySelectorAll('[data-dev-scenario-control]')).toHaveLength(0);
    expect(panel.querySelectorAll('[data-mock-scenario]')).toHaveLength(0);
    expect(panel.querySelector('[data-org-dev-data]')).toBeNull();
    expect(panel.querySelector('.dev-session-toolbar')).toBeNull();
    expect(within(panel).queryByRole('button', { name: '切换为受邀成员' })).not.toBeInTheDocument();

    await user.click(within(panel).getByRole('list', { name: '开发模块索引' }));

    expect(window.location.search).toBe('?keep=1');
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('removes the layer on unmount', async () => {
    const handle = await mount(BACKEND);
    expect(document.querySelector('.dev-tools-host')).not.toBeNull();

    await act(async () => {
      handle.unmount();
    });

    expect(document.querySelector('.dev-tools-host')).toBeNull();
    expect(screen.queryByRole('button', { name: '开发工具' })).not.toBeInTheDocument();
  });
});

describe('mountDevTools guards', () => {
  it('refuses a container inside the product #root', () => {
    const inside = document.createElement('div');
    productRoot.appendChild(inside);

    expect(() => mountDevTools(inside, BACKEND)).toThrow(/outside the product #root/);
    expect(() => mountDevTools(productRoot, BACKEND)).toThrow(/outside the product #root/);
    expect(document.querySelector('.dev-tools-host')).toBeNull();
  });

  it('refuses a second mount into the same container until unmounted', async () => {
    const handle = await mount(BACKEND);
    const container = document.getElementById(DEV_TOOLS_CONTAINER_ID)!;

    expect(() => mountDevTools(container, BACKEND)).toThrow(/already mounted/);

    await act(async () => {
      handle.unmount();
    });
    await mount(BACKEND);
    expect(screen.getAllByRole('button', { name: '开发工具' })).toHaveLength(1);
  });

  it('reuses the existing sibling container', () => {
    const first = ensureDevToolsContainer(productRoot);
    expect(ensureDevToolsContainer(productRoot)).toBe(first);
    expect(document.querySelectorAll(`#${DEV_TOOLS_CONTAINER_ID}`)).toHaveLength(1);
  });
});

describe('mountDevTools over the fixture context', () => {
  it('mounts the same layer with the fixture tools operable', async () => {
    const user = userEvent.setup({ delay: null });
    await mount(createFixtureDevToolsContext());

    const ball = screen.getByRole('button', { name: '开发工具' });
    expect(ball.closest('[data-dev-tools]')).toHaveAttribute('data-dev-tools-source', 'simulated');
    const panel = await openPanel(user);

    expect(within(panel).getByRole('note', { name: '开发预览数据来源' })).toBeInTheDocument();
    expect(within(panel).getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['当前旅程', '归档场景']);
    expect(within(panel).getByRole('tab', { name: '当前旅程' })).toHaveAttribute('aria-selected', 'true');
    expect(panel.querySelectorAll('[data-dev-scenario-control]')).toHaveLength(2);
    expect(within(panel).queryAllByRole('combobox')).toHaveLength(0);

    const order = Array.from(panel.children).map((child) => child.className || child.getAttribute('role'));
    expect(order).toEqual(['dev-data-source', 'dev-tools-tabs', 'tabpanel']);
    expect(panel.querySelector('[role="tabpanel"] > .dev-tools-presets')).not.toBeNull();

    await user.click(within(panel).getByRole('tab', { name: '归档场景' }));
    expect(within(panel).getAllByRole('combobox')).toHaveLength(7);
    expect(within(panel).getByRole('link', { name: '打开归档场景 ↗' })).toBeInTheDocument();
    expect(panel.querySelectorAll('[data-dev-scenario-control]')).toHaveLength(0);
  });

  it('keeps the selected scenario tab while the panel is closed', async () => {
    const user = userEvent.setup({ delay: null });
    await mount(createFixtureDevToolsContext());
    const panel = await openPanel(user);
    await user.click(within(panel).getByRole('tab', { name: '归档场景' }));
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '开发工具面板' })).not.toBeInTheDocument();

    const reopened = await openPanel(user);
    expect(within(reopened).getByRole('tab', { name: '归档场景' })).toHaveAttribute('aria-selected', 'true');
  });
});
