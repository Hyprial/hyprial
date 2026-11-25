import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { OrgTopology } from '../../../src/features/network/impl/OrgTopology';
import {
  buildHiddenTopologyModel,
  buildUnavailableMonitor,
  buildUnconfirmedTopologyModel,
  TOPOLOGY_COPY,
  type TopologyModel,
} from '../../../src/features/network/impl/topology-view-model';

const MONITOR_DEMO_COPY = {
  title: '最近通信记录',
} as const;

const STRUCTURE_ONLY: TopologyModel = {
  dataMode: 'structure-only',
  nodes: [
    { id: 'member-1', kind: 'member', label: '成员一', meta: '所有者' },
    { id: 'machine-1', kind: 'machine', label: 'studio-mac', meta: 'macOS 合成系统' },
  ],
  edges: [],
  emptyMessage: null,
  unavailableMessage: null,
  monitor: buildUnavailableMonitor(),
};

const FULL: TopologyModel = {
  ...STRUCTURE_ONLY,
  dataMode: 'full',
  edges: [{ id: 'edge-1', fromId: 'member-1', toId: 'machine-1', status: 'in_flight' }],
  monitor: {
    mode: 'linked',
    title: MONITOR_DEMO_COPY.title,
    sample: {
      fromLabel: '成员一',
      toLabel: 'studio-mac',
      status: 'completed',
      observedAt: '2026-10-20 09:30',
    },
  },
};

const EMPTY: TopologyModel = {
  ...STRUCTURE_ONLY,
  dataMode: 'empty',
  emptyMessage: TOPOLOGY_COPY.emptyHint,
};

const css = readFileSync(
  resolve(process.cwd(), 'src/features/network/impl/topology.css'),
  'utf8',
);

describe('J7 topology model', () => {
  it('keeps hidden distinguishable from unconfirmed', () => {
    const model = buildHiddenTopologyModel();

    expect(model.dataMode).toBe('hidden');
    expect(model.unavailableMessage).toBeNull();
  });

  it('exposes an unconfirmed model with retry copy and no success wording', () => {
    const model = buildUnconfirmedTopologyModel();

    expect(model.dataMode).toBe('unconfirmed');
    expect(TOPOLOGY_COPY.unconfirmed).toBe('状态未确认');
    expect(TOPOLOGY_COPY.retry).toBe('重试');
    expect(JSON.stringify(model)).not.toMatch(/刷新|变化/);
  });
});

describe('J7 topology rendering', () => {
  it('renders nothing for the hidden mode and adds no second failure notice', () => {
    const { container } = render(<OrgTopology model={buildHiddenTopologyModel()} />);

    expect(container).toBeEmptyDOMElement();
    expect(document.body.textContent).not.toContain(TOPOLOGY_COPY.unconfirmed);
    expect(document.body.textContent).not.toContain('通信数据暂不可用');
  });

  it('roots both section variants in the .org-topology class', () => {
    const { container: unconfirmed, unmount } = render(
      <OrgTopology model={buildUnconfirmedTopologyModel()} />,
    );
    expect(unconfirmed.querySelector('.org-topology')).not.toBeNull();
    unmount();

    const { container: diagram } = render(<OrgTopology model={FULL} />);
    const root = diagram.querySelector('.org-topology') as HTMLElement;
    expect(root).not.toBeNull();
    expect(root.getAttribute('data-topology-mode')).toBe('full');
  });

  it('renders the unconfirmed state with a retry and without a diagram', () => {
    render(<OrgTopology model={buildUnconfirmedTopologyModel()} />);

    expect(screen.getByText(TOPOLOGY_COPY.unconfirmed)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: TOPOLOGY_COPY.retry })).toBeInTheDocument();
    expect(document.querySelectorAll('[data-topology-node]')).toHaveLength(0);
    expect(document.querySelector('[data-topology-edge]')).toBeNull();
  });

  it('shows structure nodes in the structure-only mode and no edges', () => {
    render(<OrgTopology model={STRUCTURE_ONLY} />);

    const nodes = document.querySelectorAll('[data-topology-node]');
    expect(nodes).toHaveLength(2);
    expect(screen.getByText('成员一')).toBeInTheDocument();
    expect(screen.getByText('studio-mac')).toBeInTheDocument();
    expect(document.querySelector('[data-topology-edge]')).toBeNull();
  });

  it('renders no communication toggle and no Monitor in the structure-only mode', () => {
    const { container } = render(<OrgTopology model={STRUCTURE_ONLY} />);

    expect(screen.queryByRole('button', { name: '通信视图' })).toBeNull();
    expect(screen.queryByRole('group', { name: '拓扑视图' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Monitor/ })).toBeNull();
    expect(container.querySelector('.topology-toolbar')).toBeNull();
    expect(container).not.toHaveTextContent('不可用');
    expect(document.querySelector('[data-topology-edge]')).toBeNull();
  });

  it('renders no communication toggle for a full model without edges', () => {
    render(<OrgTopology model={{ ...FULL, edges: [] }} />);

    expect(screen.queryByRole('button', { name: '通信视图' })).toBeNull();
  });

  it('renders exactly one communication edge in the full mode', async () => {
    const user = userEvent.setup();
    render(<OrgTopology model={FULL} />);

    expect(document.querySelectorAll('[data-topology-view]')).toHaveLength(2);
    expect(document.querySelectorAll('[data-topology-edge]')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: '通信视图' }));
    expect(document.querySelectorAll('[data-topology-edge]')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Monitor' })).toBeEnabled();
  });

  it('also opens Inspect with the Space key', async () => {
    const user = userEvent.setup();
    render(<OrgTopology model={FULL} />);

    await user.click(screen.getByRole('button', { name: '通信视图' }));
    const edge = document.querySelector('[data-topology-edge]') as HTMLElement;
    edge.focus();
    await user.keyboard(' ');

    const panel = document.querySelector('[data-topology-panel="inspect"]');
    expect(panel).not.toBeNull();
  });

  it('shows the empty hint with injected nodes and no fabricated edges', () => {
    render(<OrgTopology model={EMPTY} />);

    expect(screen.getByText(TOPOLOGY_COPY.emptyHint)).toBeInTheDocument();
    expect(document.querySelectorAll('[data-topology-node]')).toHaveLength(2);
    expect(document.querySelector('[data-topology-edge]')).toBeNull();
  });

  it('keeps the unconfirmed state after retry without requests or storage', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    render(<OrgTopology model={buildUnconfirmedTopologyModel()} onRetry={onRetry} />);

    await user.click(screen.getByRole('button', { name: TOPOLOGY_COPY.retry }));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByText(TOPOLOGY_COPY.unconfirmed)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/刷新|变化/);
    expect(document.querySelectorAll('[data-topology-node]')).toHaveLength(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    vi.unstubAllGlobals();
  });
});

describe('J7 communication and panels', () => {
  it('opens Inspect from a keyboard-selected edge and returns focus on Escape', async () => {
    const user = userEvent.setup();
    render(<OrgTopology model={FULL} />);

    await user.click(screen.getByRole('button', { name: '通信视图' }));
    const edge = document.querySelector('[data-topology-edge]') as HTMLElement;
    expect(edge).not.toBeNull();
    expect(edge.tagName).toBe('BUTTON');

    edge.focus();
    await user.keyboard('{Enter}');

    const panel = document.querySelector('[data-topology-panel]') as HTMLElement;
    expect(panel).not.toBeNull();
    expect(within(panel).getByText('发起方')).toBeInTheDocument();
    expect(within(panel).getByText('接收方')).toBeInTheDocument();
    expect(within(panel).getByText('状态')).toBeInTheDocument();
    expect(within(panel).getByText('时间')).toBeInTheDocument();
    expect(panel.textContent).not.toMatch(/https?:\/\/|127\.0\.0\.1|socket|\d+\.\d+\.\d+\.\d+/);

    await user.keyboard('{Escape}');
    expect(document.querySelector('[data-topology-panel]')).toBeNull();
    expect(edge).toHaveFocus();
  });

  it('opens and closes Monitor with the recent communication record', async () => {
    const user = userEvent.setup();
    render(<OrgTopology model={FULL} />);

    const monitor = screen.getByRole('button', { name: /Monitor/ });
    await user.click(monitor);

    const panel = document.querySelector('[data-topology-panel]') as HTMLElement;
    expect(panel).not.toBeNull();
    expect(within(panel).getByText(MONITOR_DEMO_COPY.title)).toBeInTheDocument();
    expect(within(panel).getByText('发起方')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(document.querySelector('[data-topology-panel]')).toBeNull();
    expect(monitor).toHaveFocus();
  });

  it('keeps the selected node across view and monitor switches', async () => {
    const user = userEvent.setup();
    render(<OrgTopology model={FULL} />);

    const node = document.querySelector('[data-topology-node]') as HTMLElement;
    await user.click(node);
    expect(node).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: '通信视图' }));
    await user.click(screen.getByRole('button', { name: '结构视图' }));
    expect(node).toHaveAttribute('aria-pressed', 'true');
  });

  it('ships local focus styles, module scrolling and no canvas', () => {
    expect(css).toMatch(/:focus-visible/);
    expect(css).toMatch(/overflow-x:\s*auto/);
    expect(css).toMatch(/max-height/);
    expect(document.querySelectorAll('canvas')).toHaveLength(0);
  });

  it('scopes every CSS selector under the .org-topology root', () => {
    const selectors: string[] = [];
    for (const match of css.matchAll(/([^{}]+)\{/g)) {
      const selector = match[1].trim();
      if (selector.startsWith('@media')) {
        continue;
      }
      selectors.push(...selector.split(',').map((part) => part.trim()));
    }

    expect(selectors.length).toBeGreaterThan(10);
    const unscoped = selectors.filter(
      (selector) => selector.length > 0 && !selector.startsWith('.org-topology'),
    );
    expect(unscoped).toEqual([]);
  });
});
