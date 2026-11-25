import { createTestAgentsApi as createAgentsApi } from '../../unified-backend/agents-test-client';
import { render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { useAgentDirectory } from '../../../src/features/agents/impl/api/use-agent-directory';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { FixtureApp } from '../../../src/dev/FixtureApp';
import { AgentList } from '../../../src/features/agents/impl/AgentList';
import { type AgentsApi } from '../../../src/features/agents/impl/api';
import { authenticatedTestActions } from '../../support/demo-baseline';
import {
  AGENTS_LIST_ROWS_DATA,
  AGENTS_LIST_ROWS_EMPTY,
  AGENTS_SECRET_GRANTS_EMPTY,
  type AgentsListRowFixture,
  type AgentsSecretGrantFixture,
} from '../../fixtures/agents/agent-list';

// Radix Select 在 jsdom 打开弹出层需要这些 API 存在（真实浏览器原生提供）。
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

interface RecordedApiCall {
  readonly method: string;
  readonly params: Record<string, unknown>;
}

interface FixtureApiOptions {
  readonly rows?: readonly AgentsListRowFixture[];
  readonly grants?: readonly AgentsSecretGrantFixture[];
  readonly providerGrants?: readonly {
    readonly grantId: string;
    readonly provider: string;
    readonly state: string;
    readonly code?: string | null;
    readonly keyId?: string | null;
    readonly owner?: string | null;
    readonly expiresAt?: number | null;
    readonly unsealable?: boolean;
  }[];
  readonly listErrors?: number | readonly string[];
  readonly holdList?: Promise<void>;
}

function fixtureAgentsApi(
  options: FixtureApiOptions = {},
): { api: AgentsApi; calls: RecordedApiCall[] } {
  const calls: RecordedApiCall[] = [];
  const errorQueue = Array.isArray(options.listErrors) ? [...options.listErrors] : [];
  const listFailuresLeft = typeof options.listErrors === 'number' ? options.listErrors : 0;
  let remainingFailures = listFailuresLeft;
  const api = createAgentsApi({
    async request(method, params) {
      calls.push({ method, params });
      if (method === 'agent.list') {
        if (options.holdList) await options.holdList;
        if (errorQueue.length > 0) {
          throw new Error(errorQueue.shift() ?? 'daemon unreachable');
        }
        if (remainingFailures > 0) {
          remainingFailures -= 1;
          throw new Error('daemon unreachable');
        }
        return {
          ok: true,
          agents: options.rows ?? AGENTS_LIST_ROWS_DATA,
          agentsWithoutRoutine: [],
        };
      }
      if (method === 'agent.secret.list') {
        return { ok: true, grants: (options.providerGrants ?? []).map(grant => ({
          actor: 'agent:owner:node:a.squire', grantId: grant.grantId, source: 'user-provider',
          entryId: 'test-key', fieldName: 'apiKey', revision: 1,
          environmentNames: ['ANTHROPIC_API_KEY'], ownerActor: null, ownerEntityToken: null,
        })) };
      }
      if (method === 'agent.provider.default') return { ok: true, provider: 'claude', previous: null };
      throw new Error(`unexpected method ${method}`);
    },
  });
  return { api, calls };
}

function renderDirectoryApp(api: AgentsApi) {
  const user = userEvent.setup();
  render(
    <FixtureApp
      dataMode="fixture"
      capabilities={{ firstRun: authenticatedTestActions(), agents: api }}
    />,
  );
  return { user };
}

async function openAgents(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole('button', { name: 'Agent' })[0]);
}

describe('agents directory (read-side wiring)', () => {
  it('updates observed runtime without navigation and stops reads on unmount', async () => {
    const { api } = fixtureAgentsApi();
    const initial = await api.list!();
    const list = vi.spyOn(api, 'list').mockResolvedValueOnce(initial).mockResolvedValue([]);
    const { result, unmount } = renderHook(() => useAgentDirectory(api));
    await waitFor(() => expect(result.current.rows).toHaveLength(initial.length));
    await waitFor(() => expect(result.current.rows).toEqual([]), { timeout: 5000 });
    expect(result.current.status).toBe('ready');
    unmount();
    const count = list.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 2200));
    expect(list).toHaveBeenCalledTimes(count);
  }, 10000);

  it('renders the loaded list grouped by owner with honest run-state badges', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    expect(await screen.findByTestId('contact-squire')).toBeInTheDocument();
    expect(screen.getByTestId('contact-researcher')).toBeInTheDocument();
    expect(screen.getByTestId('contact-writer')).toBeInTheDocument();
    expect(screen.getByTestId('contact-archivist')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /未提供/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /alice/ })).toBeInTheDocument();
    expect(screen.queryByText(/找到 \d+ 个，共 \d+ 个/)).toBeNull();

    const squireCard = screen.getByTestId('contact-squire');
    expect(within(squireCard).getAllByText('运行中')).toHaveLength(1);
    expect(within(squireCard).queryByText('在线')).not.toBeInTheDocument();
    expect(within(squireCard).queryByText('已连接')).not.toBeInTheDocument();
    expect(
      within(screen.getByTestId('contact-researcher')).getAllByText('未运行'),
    ).toHaveLength(1);
    expect(
      within(screen.getByTestId('contact-researcher')).queryByText('离线'),
    ).not.toBeInTheDocument();

    expect(calls.map((call) => call.method)).toEqual(['agent.list', 'agent.list']);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('shows a busy state while loading and never a fake empty list', async () => {
    let release!: () => void;
    const holdList = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { api } = fixtureAgentsApi({ holdList });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    expect(await screen.findByText('Agent 列表加载中…')).toBeInTheDocument();
    expect(screen.queryByText(/还没有 Agent/)).not.toBeInTheDocument();

    release();
    expect(await screen.findByTestId('contact-squire')).toBeInTheDocument();
  });

  it('shows the fixed directory error with retry and hides raw text', async () => {
    const { api, calls } = fixtureAgentsApi({
      listErrors: [
        'HTTP 503: reference capability unavailable or refused',
        'HTTP 503: reference capability unavailable or refused',
        'HTTP 503: reference capability unavailable or refused',
      ],
    });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    expect(await screen.findByRole('heading', { name: 'Agent 列表读取失败' })).toBeInTheDocument();
    expect(screen.getByText('错误码 503')).toBeInTheDocument();
    expect(screen.queryByText(/reference capability unavailable/u)).toBeNull();
    expect(screen.queryByText('还没有 Agent')).toBeNull();
    expect(screen.queryByRole('button', { name: /创建 Agent/u })).toBeNull();
    expect(screen.queryByRole('searchbox')).toBeNull();

    await user.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByRole('heading', { name: 'Agent 列表读取失败' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByTestId('contact-squire')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Agent 列表读取失败' })).toBeNull();
    expect(calls.filter((call) => call.method === 'agent.list')).toHaveLength(4);
  });

  it('keeps the create entry available with an empty list', async () => {
    const { api } = fixtureAgentsApi({ rows: AGENTS_LIST_ROWS_EMPTY });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    expect(await screen.findByText(/还没有 Agent/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
  });

  it('renders create as wired in the loaded list (implG2)', async () => {
    const { api } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    const create = await screen.findAllByRole('button', { name: '创建 Agent' });
    for (const button of create) expect(button).toBeEnabled();
  });

  it('opens the detail with runtime facts, grants and provider status from the read api', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    await user.click(await screen.findByTestId('contact-squire'));

    const detail = await screen.findByTestId('agent-detail-panel');
    expect(within(detail).getByTestId('agent-detail-runstate')).toHaveTextContent('空闲');
    const harnessModel = within(detail).getByTestId('overview-harness');
    expect(harnessModel.textContent).toContain('pi');
    expect(within(detail).getByTestId('overview-model').textContent).toContain('claude-sonnet');
    expect(calls.map((call) => call.method)).toEqual([
      'agent.list',
      'agent.list',
      'agent.secret.list',
    ]);

    expect(within(detail).getByRole('button', { name: '开始对话' })).toBeEnabled();
    expect(within(detail).getByRole('button', { name: '删除' })).toBeEnabled();

    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    expect(await within(detail).findByText('无 Key 授权。')).toBeInTheDocument();
    expect(calls.map((call) => call.method)).toContain('agent.secret.list');
  });

  it('shows confirmed absence of provider grants without inventing one', async () => {
    const { api } = fixtureAgentsApi({ grants: AGENTS_SECRET_GRANTS_EMPTY });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    await user.click(await screen.findByTestId('contact-researcher'));

    const detail = await screen.findByTestId('agent-detail-panel');
    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    expect(
      await within(detail).findByText('无 Key 授权。'),
    ).toBeInTheDocument();
    expect(within(detail).getByRole('button', { name: '开始对话' })).toBeEnabled();
  });

  it('shows Key grants without claiming a usable credential (G1-1)', async () => {
    const { api } = fixtureAgentsApi({
      providerGrants: [{ grantId: 'claude', provider: 'claude', state: 'ok' }],
    });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    await user.click(await screen.findByTestId('contact-squire'));

    const detail = await screen.findByTestId('agent-detail-panel');
    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    const keys = await within(detail).findByRole('list', { name: 'Key 授权' });
    expect(within(keys).getAllByText('已授权').length).toBeGreaterThan(0);
    expect(within(detail).queryByText('已连接')).not.toBeInTheDocument();
    expect(
      within(detail).getByRole('button', { name: '开始对话' }),
    ).toBeEnabled();
  });

  it('enables 开始对话 as navigation without claiming credential availability (G1-1)', async () => {
    const chatTargetRow: AgentsListRowFixture = {
      actor: 'agent-squire',
      uri: 'agent:owner:node:a.squire',
      owner: '',
      machine: '',
      provider: 'claude',
      model: 'claude-sonnet',
      harness: 'pi',
      preferredHarness: 'pi',
      runState: {
        state: 'idle',
        certainty: 'observed',
        observationHealth: 'current',
      },
    };
    const { api } = fixtureAgentsApi({
      rows: [chatTargetRow],
      providerGrants: [{ grantId: 'claude', provider: 'claude', state: 'ok' }],
    });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    await user.click(await screen.findByTestId('contact-agent-squire'));

    const detail = await screen.findByTestId('agent-detail-panel');
    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    const keys = await within(detail).findByRole('list', { name: 'Key 授权' });
    expect(within(keys).getAllByText('已授权').length).toBeGreaterThan(0);
    expect(within(detail).queryByText('已连接')).not.toBeInTheDocument();
    expect(
      within(detail).getByRole('button', { name: '开始对话' }),
    ).toBeEnabled();
  });

  it('finds agents by their actual model (G1-2)', async () => {
    const { api } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);

    expect(await screen.findByTestId('contact-squire')).toBeInTheDocument();
    await user.type(
      screen.getByRole('searchbox', { name: '搜索 Agent' }),
      'claude-sonnet',
    );

    expect(screen.getByTestId('contact-squire')).toBeInTheDocument();
    expect(screen.queryByTestId('contact-researcher')).not.toBeInTheDocument();
    expect(screen.getByText(/找到 \d+ 个，共 \d+ 个/)).toBeInTheDocument();
  });

  it('never routes reads through the stub adapter', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await user.click(await screen.findByTestId('contact-squire'));
    await screen.findByTestId('agent-detail-panel');

    expect(calls.map((call) => call.method)).toEqual([
      'agent.list',
      'agent.list',
      'agent.secret.list',
    ]);
  });
});


describe('create entry availability', () => {
  const baseProps = {
    agents: [], query: '', filter: 'all' as const,
    ownerFilter: 'all', hostFilter: 'all', ownerOptions: [], hostOptions: [],
    onOwnerFilter: () => {}, onHostFilter: () => {}, onCreate: () => {},
    onOpen: () => {}, onQuery: () => {}, onFilter: () => {}, onClearFilters: () => {},
  };

  it('renders no create control when the list has no way to create, and no explanation either', () => {
    render(<AgentList {...baseProps} total={3} onCreate={undefined} />);
    expect(screen.queryByRole('button', { name: /创建 Agent/ })).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂不可用|尚未开放/);
  });

  it('renders an enabled create entry without title or alternate name when it can create', () => {
    render(<AgentList {...baseProps} total={3} />);
    const create = screen.getByRole('button', { name: '创建 Agent' });
    expect(create).toBeEnabled();
    expect(create).not.toHaveAttribute('title');
    expect(create).not.toHaveAttribute('aria-label');
  });

  it('keeps the create entry for an empty organization and points at it', () => {
    render(<AgentList {...baseProps} total={0} />);
    expect(screen.getByRole('button', { name: '创建 Agent' })).toBeEnabled();
    expect(screen.getByText('还没有 Agent。使用上方“创建 Agent”开始。')).toBeInTheDocument();
  });

  it('an empty list without a create entry does not point at one', () => {
    render(<AgentList {...baseProps} total={0} onCreate={undefined} />);
    expect(screen.queryByRole('button', { name: /创建 Agent/ })).toBeNull();
    expect(screen.getByText('还没有 Agent。')).toBeInTheDocument();
  });
});

 it('does not add a clear action for a runtime filter, including an empty result', () => {
  render(<AgentList agents={[]} total={1} query="" filter="available" availabilityMode="runtime"
    ownerFilter="" hostFilter="" ownerOptions={[]} hostOptions={[]}
    onOwnerFilter={() => {}} onHostFilter={() => {}} onCreate={() => {}} onOpen={() => {}}
    onQuery={() => {}} onFilter={() => {}} onClearFilters={() => {}} />);
  expect(screen.getByRole('button', { name: '全部' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '清除筛选' })).toBeNull();
});
