import { createTestAgentsApi as createAgentsApi } from '../../unified-backend/agents-test-client';
/**
 * implG4 (2026-11-02)：四页签 Agent 详情（概览 | 接入 | 凭据 | 关联）。
 *
 * - 头部常显：所有者 / 主机 / harness·模型 / 所属组织 / 平台绑定 / 运行状态，
 *   停止与开始对话按钮；页签切换不改数据权威。
 * - 凭据页签：只读当前 Key 授权与 Auth 不可读取事实；编辑经“编辑凭据”弹窗完成，
 *   授权记录不证明凭据可用。
 * - 页签随能力出现：平台绑定需要 adapterBindings，凭据需要 agentsApi，所属组织
 *   需要 orgSections；没有对应来源的页签不渲染，不再有禁用页签和预留卡。
 * - 概览：已移除 link.list 调用与连接记录；组织复用 dev-04
 *   AgentOrgPanel。
 */

import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { FixtureApp } from '../../../src/dev/FixtureApp';
import { AgentDetail } from '../../../src/features/agents/impl/AgentDetail';
import { getAgentExtension } from '../../../src/features/agents/impl/agent-extensions';
import {
  type AgentsApi,
} from '../../../src/features/agents/impl/api';
import { MethodRefusedError } from '../../../src/client';
import { AgentOrgPanel } from '../../../src/features/org';
import { authenticatedTestActions } from '../../support/demo-baseline';

import {
  AGENTS_DETAIL_AVAILABLE,
} from '../../fixtures/agents/agent-detail';
import {
  AGENTS_DETAIL_ROWS_HOME,
  AGENTS_ROUTINE_ROWS,
  AGENTS_ROUTINE_STATUS,
  AGENTS_WORKFLOW_ROWS,
  extensionWithGrantStates,
  routineListResponse,
  routineStatusResponse,
  workflowListResponse,
  type AgentsRoutineRowFixture,
  type AgentsRoutineStatusFixture,
  type AgentsWorkflowRunFixture,
} from '../../fixtures/agents/agent-detail-tabs';
import {
  AGENTS_LIST_ROWS_EMPTY,
  type AgentsListRowFixture,
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
  readonly providerStatusError?: Error;
  readonly routineRows?: readonly AgentsRoutineRowFixture[];
  readonly routinesError?: Error;
  readonly routineStatus?: AgentsRoutineStatusFixture;
  readonly routineStatusError?: Error;
  readonly workflowRows?: readonly AgentsWorkflowRunFixture[];
  readonly workflowsError?: Error;
  readonly startResult?: Record<string, unknown>;
  readonly startError?: unknown;
  readonly providerGrants?: readonly {
    readonly grantId: string;
    readonly provider: string;
    readonly state: string;
    readonly keyId?: string | null;
  }[];
}

function fixtureAgentsApi(
  options: FixtureApiOptions = {},
): { api: AgentsApi; calls: RecordedApiCall[] } {
  const calls: RecordedApiCall[] = [];
  const api = createAgentsApi({
    async request(method, params) {
      calls.push({ method, params });
      if (method === 'agent.list') {
        return {
          ok: true,
          agents: options.rows ?? AGENTS_DETAIL_ROWS_HOME,
          agentsWithoutRoutine: [],
        };
      }
      if (method === 'agent.rename') {
        const rows = options.rows ?? AGENTS_DETAIL_ROWS_HOME;
        const target = rows.find((row) => row.actor === params.name) ?? rows[0];
        return {
          ok: true,
          agent: { ...target, displayName: String(params.displayName) },
        };
      }
      if (method === 'agent.secret.list') {
        if (options.providerStatusError) throw options.providerStatusError;
        return {
          ok: true,
          actor: 'agent:owner:node:a.researcher',
          agent: 'a.researcher',
          agentId: 'a.researcher',
          grants: (options.providerGrants ?? []).map(grant => ({
            actor: 'agent:owner:node:a.researcher', grantId: grant.grantId, source: 'user-provider',
            entryId: 'test-key', fieldName: 'apiKey', revision: 1,
            environmentNames: [grant.provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'DEEPSEEK_API_KEY'],
            ownerActor: null, ownerEntityToken: null,
          })),
        };
      }
      if (method === 'agent.stop') {
        return { ok: true, removed: ['claude:researcher'] };
      }
      if (method === 'agent.start') {
        if (options.startError) throw options.startError;
        return options.startResult ?? { ok: true, accepted: true, running: true };
      }
      if (method === 'routine.list') {
        if (options.routinesError) throw options.routinesError;
        return routineListResponse(options.routineRows ?? AGENTS_ROUTINE_ROWS);
      }
      if (method === 'routine.status') {
        if (options.routineStatusError) throw options.routineStatusError;
        return routineStatusResponse(options.routineStatus ?? AGENTS_ROUTINE_STATUS);
      }
      if (method === 'workflow.list') {
        if (options.workflowsError) throw options.workflowsError;
        return workflowListResponse(options.workflowRows ?? AGENTS_WORKFLOW_ROWS);
      }
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

async function openDetail(
  user: ReturnType<typeof userEvent.setup>,
  contactId = 'contact-researcher',
) {
  await user.click(screen.getAllByRole('button', { name: 'Agent' })[0]);
  await user.click(await screen.findByTestId(contactId));
  return await screen.findByTestId('agent-detail-panel');
}

function detailRender(overrides: {
  extension?: ReturnType<typeof extensionWithGrantStates>;
  onStartChat?: () => void;
  providerStatus?: 'ok' | 'pending' | 'error' | 'unavailable';
  withoutApi?: boolean;
  withRuntime?: boolean;
  providerGrants?: readonly {
    readonly grantId: string;
    readonly provider: string;
    readonly state: string;
    readonly keyId?: string | null;
  }[];
}) {
  const user = userEvent.setup();
  const onNavigateConnections = vi.fn();
  const api = createAgentsApi({
    async request(method) {
      throw new Error(`unexpected method ${method}`);
    },
  });
  api.providerStatus = async () => {
    if (overrides.providerStatus === 'pending') return new Promise(() => {});
    if (overrides.providerStatus === 'error') throw new Error('provider status failed');
    if (overrides.providerStatus === 'unavailable') throw new MethodRefusedError('agent.secret.list', 'METHOD_NOT_AVAILABLE');
    return (overrides.providerGrants ?? []).map(grant => ({
      code: null, keyId: null, owner: null, expiresAtMs: null, unsealable: false, ...grant,
    })) as Awaited<ReturnType<NonNullable<AgentsApi['providerStatus']>>>;
  };

  render(
    <AgentDetail
      agent={AGENTS_DETAIL_AVAILABLE}
      extension={
        overrides.extension ??
        extensionWithGrantStates([
          { provider: 'anthropic', state: 'ok', grantedAtLabel: '10-28 14:02' },
        ])
      }
      organizations={[]}
      onNavigateConnections={onNavigateConnections}
      onStartChat={overrides.onStartChat}
      onRuntimeAction={overrides.withRuntime
        ? async () => ({ tone: 'status', text: '已启动', dispatched: true })
        : undefined}
      agentsApi={overrides.withoutApi ? undefined : api}
    />,
  );
  return { user, onNavigateConnections };
}

describe('agent detail four-tab shell (implG4)', () => {
  it('uses one lifecycle button and does not replay start while waiting for the observed state', async () => {
    const user = userEvent.setup();
    let finish!: (value: { tone: 'status'; text: string; dispatched: boolean; awaitingObservation: boolean }) => void;
    const action = vi.fn((_kind: 'start' | 'stop') => new Promise<{ tone: 'status'; text: string; dispatched: boolean; awaitingObservation: boolean }>(resolve => { finish = resolve; }));
    const refresh = vi.fn();
    const props = {
      agent: AGENTS_DETAIL_AVAILABLE, organizations: [], onNavigateConnections: vi.fn(),
      onRuntimeAction: action, onRefreshRuntime: refresh,
    };
    const view = render(<AgentDetail {...props}
      extension={extensionWithGrantStates([], { runtime: 'offline', preferredHarness: 'claude' })} />);
    expect(screen.queryByRole('button', { name: '停止' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '启动' }));
    await user.click(screen.getByRole('button', { name: '保存并启动' }));
    expect(screen.getByRole('button', { name: '启动中…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '设置模型…' })).toBeDisabled();
    await act(async () => finish({ tone: 'status', text: '已受理', dispatched: true, awaitingObservation: true }));
    await user.click(screen.getByRole('button', { name: '刷新状态' }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(action).toHaveBeenCalledTimes(1);
    view.rerender(<AgentDetail {...props}
      extension={extensionWithGrantStates([], { runtime: 'online', preferredHarness: 'claude' })} />);
    expect(screen.getByRole('button', { name: '停止' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '启动' })).toBeNull();
    expect(screen.getByRole('button', { name: '设置模型…' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '停止' }));
    expect(screen.getByRole('button', { name: '停止中…' })).toBeDisabled();
    expect(action.mock.calls.at(-1)?.[0]).toBe('stop');
  });
  it('keeps the creation harness fixed and does not offer an Auth vendor override', async () => {
    const { user } = detailRender({ withRuntime: true,
      extension: extensionWithGrantStates([], { runtime: 'offline', preferredHarness: 'codex' }) });
    await user.click(screen.getByRole('button', { name: '设置模型…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    expect(within(dialog).queryByRole('combobox', { name: 'harness' })).toBeNull();
    expect(within(dialog).getByLabelText('harness')).toHaveValue('codex');
    expect(within(dialog).getByLabelText('harness')).toHaveAttribute('readonly');
    expect(within(dialog).queryByLabelText(/服务商/)).toBeNull();
    expect(within(dialog).queryByPlaceholderText('留空使用已保存配置')).toBeNull();
  });
  it('requires a model with an explicit Pi provider before dispatching start', async () => {
    const { user } = detailRender({ withRuntime: true,
      extension: extensionWithGrantStates([], { runtime: 'offline', preferredHarness: 'pi' }) });
    await user.click(screen.getByRole('button', { name: '设置模型…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    await user.type(within(dialog).getByLabelText(/服务商/), 'kimi-coding');
    expect(within(dialog).getByRole('button', { name: '保存并启动' })).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^模型/), 'kimi-for-coding');
    await user.clear(within(dialog).getByLabelText('工作目录'));
    await user.type(within(dialog).getByLabelText('工作目录'), '/tmp/project');
    await user.click(within(dialog).getByRole('checkbox', { name: '信任此工作目录并允许自动恢复' }));
    expect(within(dialog).getByRole('button', { name: '保存并启动' })).toBeEnabled();
  });
  it('requires vendor/model for a Hyprial Key before dispatch', async () => {
    const { api, calls } = fixtureAgentsApi({ rows: [{ ...AGENTS_DETAIL_ROWS_HOME[0],
      provider: 'hyprial', model: null, preferredHarness: 'claude',
      runState: { state: 'not-running', certainty: 'observed', observationHealth: 'current' } }] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    await user.click(screen.getByRole('button', { name: '设置模型…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    const submit = within(dialog).getByRole('button', { name: '保存并启动' });
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^模型/), 'deepseek-flash');
    expect(submit).toBeDisabled();
    await user.clear(within(dialog).getByLabelText(/^模型/));
    await user.type(within(dialog).getByLabelText(/^模型/), 'deepseek/deepseek-flash');
    await user.click(submit);
    expect(calls.find(call => call.method === 'agent.start')?.params.model).toBe('deepseek/deepseek-flash');
  });
  it('edits the current working directory in the same start request', async () => {
    const { api, calls } = fixtureAgentsApi({ rows: [{ ...AGENTS_DETAIL_ROWS_HOME[0],
      cwd: '/tmp/old-project', provider: null, model: null, preferredHarness: 'codex',
      runState: { state: 'not-running', certainty: 'observed', observationHealth: 'current' } }] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    await user.click(screen.getByRole('button', { name: '设置工作目录…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    const cwd = within(dialog).getByLabelText('工作目录');
    expect(cwd).toHaveValue('/tmp/old-project');
    await user.clear(cwd);
    await user.type(cwd, '/tmp/new-project');
    await user.click(within(dialog).getByRole('button', { name: '保存并启动' }));
    expect(calls.filter(call => call.method === 'agent.start')).toHaveLength(1);
    expect(calls.find(call => call.method === 'agent.start')?.params.cwd).toBe('/tmp/new-project');
    expect(calls.some(call => call.method === 'agent.create')).toBe(false);
  });
  it('requires Pi directory approval, resets it on edit and reopen, and sends it with cwd', async () => {
    const { api, calls } = fixtureAgentsApi({ rows: [{ ...AGENTS_DETAIL_ROWS_HOME[0],
      cwd: '/tmp/old-project', provider: null, model: null, preferredHarness: 'pi',
      runState: { state: 'not-running', certainty: 'observed', observationHealth: 'current' } }] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    await user.click(screen.getByRole('button', { name: '启动' }));
    let dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    expect(within(dialog).getByRole('button', { name: '保存并启动' })).toBeDisabled();
    const consent = () => within(dialog).getByRole('checkbox', { name: '信任此工作目录并允许自动恢复' });
    await user.click(consent());
    expect(within(dialog).getByRole('button', { name: '保存并启动' })).toBeEnabled();
    await user.clear(within(dialog).getByLabelText('工作目录'));
    expect(consent()).not.toBeChecked();
    await user.click(consent());
    expect(within(dialog).getByRole('button', { name: '保存并启动' })).toBeDisabled();
    await user.type(within(dialog).getByLabelText('工作目录'), '/tmp/new-project');
    expect(consent()).not.toBeChecked();
    await user.click(consent());
    await user.click(within(dialog).getByRole('button', { name: '取消' }));
    await user.click(screen.getByRole('button', { name: '启动' }));
    dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    expect(consent()).not.toBeChecked();
    await user.clear(within(dialog).getByLabelText('工作目录'));
    await user.type(within(dialog).getByLabelText('工作目录'), '/tmp/new-project');
    await user.click(consent());
    await user.click(within(dialog).getByRole('button', { name: '保存并启动' }));
    expect(calls.filter(call => call.method === 'agent.start')).toEqual([
      { method: 'agent.start', params: { name: AGENTS_DETAIL_ROWS_HOME[0].actor,
        harness: 'pi', cwd: '/tmp/new-project', approve: true } },
    ]);
  });
  it('prefills the Codex DeepSeek model and refuses an unsupported replacement', async () => {
    const { api, calls } = fixtureAgentsApi({ rows: [{ ...AGENTS_DETAIL_ROWS_HOME[0],
      provider: 'deepseek', model: null, preferredHarness: 'codex',
      runState: { state: 'not-running', certainty: 'observed', observationHealth: 'current' } }] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    await user.click(screen.getByRole('button', { name: '设置模型…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    const model = within(dialog).getByLabelText(/^模型/);
    const submit = within(dialog).getByRole('button', { name: '保存并启动' });
    expect(model).toHaveValue('deepseek-flash');
    await user.clear(model);
    await user.type(model, 'deepseek-reasoner');
    expect(submit).toBeDisabled();
    expect(calls.some(call => call.method === 'agent.start')).toBe(false);
    await user.clear(model);
    await user.type(model, 'deepseek-flash');
    await user.click(submit);
    expect(calls.find(call => call.method === 'agent.start')?.params).toMatchObject({
      harness: 'codex', provider: 'deepseek', model: 'deepseek-flash',
    });
  });
  it('prefills the Pi DeepSeek model when the directory has no model value', async () => {
    const { api } = fixtureAgentsApi({ rows: [{ ...AGENTS_DETAIL_ROWS_HOME[0],
      provider: 'deepseek', model: null, preferredHarness: 'pi',
      runState: { state: 'not-running', certainty: 'observed', observationHealth: 'current' } }] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    await user.click(screen.getByRole('button', { name: '设置模型…' }));
    const dialog = screen.getByRole('dialog', { name: '启动 Agent' });
    expect(within(dialog).getByLabelText(/^模型/)).toHaveValue('deepseek-flash');
  });
  it('always shows the header facts with run state, stop and 开始对话', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    const facts = detail.querySelector('dl.agent-detail-facts');
    expect(facts).not.toBeNull();
    expect(within(facts as HTMLElement).getByText('所有者')).toBeInTheDocument();
    expect(within(facts as HTMLElement).getByText('主机')).toBeInTheDocument();
    expect(within(detail).getByTestId('overview-harness')).toBeInTheDocument();
    expect(within(facts as HTMLElement).queryByText('所属组织')).toBeNull();
    expect(within(facts as HTMLElement).queryByText('平台绑定')).toBeNull();
    expect(within(detail).getByTestId('agent-detail-runstate')).toHaveTextContent(
      '空闲',
    );
    expect(within(detail).getByRole('button', { name: '停止' })).toBeEnabled();
    const startChat = within(detail).getByRole('button', { name: '开始对话' });
    expect(startChat).toBeEnabled();

    const tablist = within(detail).getByRole('tablist');
    expect(within(tablist).getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['概览', '凭据']);
    expect(within(tablist).getByRole('tab', { name: '概览' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    expect(calls.map((call) => call.method)).toEqual([
      'agent.list',
      'agent.list',
      'agent.secret.list',
    ]);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('configures the name inline and has no separate edit form', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    expect(within(detail).queryByRole('button', { name: '编辑' })).toBeNull();
    await user.click(within(detail).getByRole('button', { name: '重命名' }));
    const input = within(detail).getByLabelText('Agent 名称');
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(within(detail).getByRole('button', { name: '保存' }));

    expect(calls.some(call => call.method === 'agent.rename')).toBe(false);
    expect(await screen.findByRole('alert')).toHaveTextContent('名称');
    await user.clear(input);
    await user.type(input, 'new-name');
    await user.click(within(detail).getByRole('button', { name: '保存' }));

    expect(await screen.findByText('已重命名')).toBeInTheDocument();
    expect(calls.find((call) => call.method === 'agent.rename')?.params).toMatchObject({
      name: 'researcher',
      displayName: 'new-name',
    });
  });

  it('shows the more-info facts without a disclosure and no home census', async () => {
    const { api } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);
    const more = within(detail).getByTestId('agent-more-info');
    expect(within(more).getByTestId('overview-harness')).toHaveTextContent('claude');
    expect(within(more).getAllByText('harness', { exact: true })).toHaveLength(1);
    expect(within(more).getByTestId('overview-model')).toHaveTextContent('claude-sonnet-4.6');
    expect(within(more).getByText('工作目录')).toBeInTheDocument();
    expect(within(more).getByTestId('overview-cwd')).toHaveTextContent('researcher');
    expect(within(more).queryByTestId('overview-home')).toBeNull();
    expect(within(detail).queryByRole('button', { name: '更多信息' })).toBeNull();
  });

  it('shows the creation harness and distinguishes an unspecified model', async () => {
    const { user } = detailRender({
      withRuntime: true,
      extension: extensionWithGrantStates([], {
        runtime: 'offline',
        preferredHarness: 'claude',
      }),
    });

    const facts = document.querySelector('dl.agent-detail-facts') as HTMLElement;
    expect(facts).not.toHaveTextContent('claude（默认）');
    expect(facts).not.toHaveTextContent('（首选）');

    const more = screen.getByTestId('agent-more-info');
    expect(within(more).queryByText('运行 harness')).toBeNull();
    expect(within(more).queryByText('创建时 harness')).toBeNull();
    expect(within(more).getByTestId('overview-harness')).toHaveTextContent('claude');
    expect(within(more).getByTestId('overview-model')).toHaveTextContent('未设置');

    await user.click(within(more).getByRole('button', { name: '设置模型…' }));
    expect(screen.getByRole('dialog', { name: '启动 Agent' })).toBeInTheDocument();
  });

  it('does not project agent.list capabilities as a user-facing capability list', async () => {
    const { api } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    expect(within(detail).queryByLabelText('能力')).toBeNull();
    expect(within(detail).queryByText('资料检索')).toBeNull();
    expect(within(detail).queryByText('能力未指定。')).toBeNull();
  });


  it('credentials tab shows Key grants and renders no edit control without a write source', async () => {
    const { api, calls } = fixtureAgentsApi({
      providerGrants: [{ grantId: 'anthropic', provider: 'anthropic', state: 'ok' }],
    });
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    await user.click(within(detail).getByRole('tab', { name: '凭据' }));

    const keyRows = await within(detail).findByTestId('agent-key-grants');
    expect(within(keyRows).getByText('anthropic')).toBeInTheDocument();
    expect(within(keyRows).getByText('已授权')).toBeInTheDocument();

    expect(within(detail).queryByTestId('credential-agent-secrets')).toBeNull();
    expect(within(detail).queryByText('能力授权')).toBeNull();
    expect(within(detail).queryByRole('button', { name: '撤销' })).toBeNull();
    expect(within(detail).queryByRole('button', { name: '编辑凭据' })).toBeNull();
    expect(calls.map((call) => call.method)).toContain('agent.secret.list');
  });

  it('credentials empty state shows no fabricated Key grant or connected state', async () => {
    const { api } = fixtureAgentsApi({ rows: AGENTS_DETAIL_ROWS_HOME });
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    await user.click(within(detail).getByRole('tab', { name: '凭据' }));

    expect(
      await within(detail).findByText('无 Key 授权。'),
    ).toBeInTheDocument();
    expect(within(detail).queryByText('已连接')).not.toBeInTheDocument();
  });

  it('keeps Key read errors inside credentials instead of treating Auth as broken', async () => {
    const { api } = fixtureAgentsApi({
      providerStatusError: new Error('provider status failed'),
    });
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    const keyError = await within(detail).findByText('Key 授权读取失败。');
    expect(keyError).not.toBeVisible();
    expect(within(detail).queryByTestId('overview-notices')).toBeNull();
    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    expect(keyError).toBeVisible();
    expect(within(detail).queryByText('无 Key 授权。')).toBeNull();
  });

  it('renders exactly the tabs its sources provide: no run tab, no disabled tab', async () => {
    const { api } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);
    expect(within(detail).queryByRole('tab', { name: '运行' })).toBeNull();
    expect(within(detail).queryByRole('tab', { name: '所属组织' })).toBeNull();
    const tabs = within(detail).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['概览', '凭据']);
    for (const tab of tabs) {
      expect(tab).toBeEnabled();
      expect(tab).not.toHaveAttribute('aria-disabled');
    }
    expect(detail.querySelectorAll('[role="tabpanel"]')).toHaveLength(2);
  });

  it('never fetches link.list or workflow.list from the overview', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    expect(screen.queryByTestId('relations-links')).toBeNull();
    expect(calls.some((call) => call.method === 'link.list')).toBe(false);
    expect(calls.some((call) => call.method === 'workflow.list')).toBe(false);
    expect(within(detail).queryByTestId('agent-more-info-link-rows')).toBeNull();
    expect(within(detail).queryByText('连接记录')).toBeNull();
  });

  it('shows no workflow UI even when the workflow source would fail', async () => {
    const { api, calls } = fixtureAgentsApi({ workflowsError: new Error('workflow down') });
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);
    expect(calls.some((call) => call.method === 'workflow.list')).toBe(false);
    expect(within(detail).queryByTestId('relations-workflows')).toBeNull();
  });

  it('renders no PAC workflow empty state because workflows are not fetched', async () => {
    const { api, calls } = fixtureAgentsApi({ workflowRows: [] });
    const { user } = renderDirectoryApp(api);
    await openDetail(user);
    expect(calls.some((call) => call.method === 'workflow.list')).toBe(false);
    expect(screen.queryByText('没有 PAC 工作流记录。')).toBeNull();
  });

  it('G4-1: the 组织 card never renders blank — explicit state line when the org source is absent', async () => {
    const user = userEvent.setup();
    render(
      <AgentDetail
        agent={AGENTS_DETAIL_AVAILABLE}
        extension={extensionWithGrantStates([])}
        organizations={[]}
        onNavigateConnections={vi.fn()}
        orgSections={
          <AgentOrgPanel
            agentId={AGENTS_DETAIL_AVAILABLE.id}
            agentName={AGENTS_DETAIL_AVAILABLE.displayName}
            dispatch={vi.fn()}
          />
        }
      />,
    );
    const detail = screen.getByTestId('agent-detail-panel');

    await user.click(within(detail).getByRole('tab', { name: '所属组织' }));

    const org = within(detail).getByTestId('relations-org');
    expect(org).toHaveAttribute('data-org-state', 'unknown');
    expect(within(org).getByText('组织状态未确认。')).toBeInTheDocument();
  });
});

describe('credential chips and notices (component level)', () => {
  it('renders every Key grant state chip without claiming availability', async () => {
    const { user } = detailRender({
      providerGrants: [
        { grantId: 'anthropic', provider: 'anthropic', state: 'ok' },
        { grantId: 'openai', provider: 'openai', state: 'recipient-pending' },
        { grantId: 'deepseek', provider: 'deepseek', state: 'unseal-failed' },
        { grantId: 'zhipu', provider: 'zhipu', state: 'missing' },
        { grantId: 'codex', provider: 'codex', state: 'revoked' },
        { grantId: 'pi', provider: 'pi', state: 'unavailable' },
      ],
    });
    await user.click(screen.getByRole('tab', { name: '凭据' }));
    const rows = await screen.findByTestId('agent-key-grants');
    for (const label of ['已授权', '等待原主人设备', '解封失败', '未授权', '已撤销', '状态不可用']) {
      expect(within(rows).getByText(label)).toBeInTheDocument();
    }
    expect(within(rows).queryByText('已连接')).not.toBeInTheDocument();
    expect(within(rows).queryByText('可用')).not.toBeInTheDocument();
  });

  it('aggregates abnormal credential states into 需要注意 with a jump link', async () => {
    const { user } = detailRender({
      providerGrants: [
        { grantId: 'openai', provider: 'openai', state: 'recipient-pending' },
        { grantId: 'deepseek', provider: 'deepseek', state: 'unseal-failed' },
      ],
    });

    const notices = await screen.findByTestId('overview-notices');
    expect(await within(notices).findByText(/等待原主人设备/)).toBeInTheDocument();
    expect(within(notices).getByText(/解封失败/)).toBeInTheDocument();

    await user.click(
      within(notices).getAllByRole('button', { name: /去凭据/ })[0],
    );
    expect(
      screen.getByRole('tab', { name: '凭据' }),
    ).toHaveAttribute('aria-selected', 'true');
  });

  it.each(['pending', 'error', 'unavailable'] as const)('keeps %s Key reads out of unrelated overview health', async (providerStatus) => {
    detailRender({ providerStatus });
    await screen.findByTestId('overview-activity');
    expect(screen.queryByTestId('overview-notices')).toBeNull();
    expect(document.body.textContent).not.toMatch(/暂无异常/);
  });

  it('does not turn an observed Secret grant into an abnormal credential', async () => {
    detailRender({ providerGrants: [{ grantId: 'model-deepseek', provider: 'deepseek', state: 'granted' }] });
    await screen.findByTestId('overview-notices');
    expect(screen.queryByText('凭据授权状态异常')).toBeNull();
    expect(screen.queryByText('已连接')).toBeNull();
  });

  it('scopes the empty notice to the loaded facts', async () => {
    detailRender({});
    expect((await screen.findByTestId('overview-notices')).textContent).toContain(
      '已读取信息中暂无异常。',
    );
  });

  it('renders no 编辑凭据 control without a credential write source', async () => {
    const { user } = detailRender({ providerGrants: [] });
    await user.click(screen.getByRole('tab', { name: '凭据' }));
    expect(screen.queryByText('暂无 Auth 状态查询。')).toBeNull();
    expect(screen.queryByRole('button', { name: '编辑凭据' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/不可用|暂不/);
  });

  it('keeps G1-1: grant inventory never claims a usable credential', () => {
    detailRender({
      extension: extensionWithGrantStates([
        { provider: 'anthropic', state: 'ok', grantedAtLabel: '10-28 14:02' },
      ]),
    });
    expect(screen.queryByText('已连接')).not.toBeInTheDocument();
    expect(screen.queryByText('可用')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '开始对话' })).toBeNull();
  });
});

describe('header stop wiring (migrated from the manage panel)', () => {
  it('stop through the header reports the removal and refreshes the directory', async () => {
    const { api, calls } = fixtureAgentsApi();
    const { user } = renderDirectoryApp(api);
    const detail = await openDetail(user);

    await user.click(within(detail).getByRole('button', { name: '停止' }));

    expect(
      await within(detail).findByText('停止已完成，已移除 1 个运行项。'),
    ).toBeInTheDocument();
    expect(calls.filter((call) => call.method === 'agent.stop')).toHaveLength(1);
    expect(calls.filter((call) => call.method === 'agent.list').length).toBe(
      3,
    );
  });

  it('renders no stop button when no runtime handler is wired', () => {
    detailRender({});
    expect(screen.queryByRole('button', { name: '停止' })).toBeNull();
  });
});

describe('agents list regression (tabs must not break the directory)', () => {
  it('the empty directory still renders the shared empty state', async () => {
    const { api } = fixtureAgentsApi({ rows: AGENTS_LIST_ROWS_EMPTY });
    const { user } = renderDirectoryApp(api);
    await user.click(screen.getAllByRole('button', { name: 'Agent' })[0]);
    expect(await screen.findByText(/还没有 Agent/)).toBeInTheDocument();
  });
});

describe('v13 · hierarchy header and link labelling', () => {
  it('hides the runtime-environment row when both values are empty', () => {
    render(
      <AgentDetail
        agent={{
          id: 'worker', displayName: 'Worker', role: '测试', avatarTone: 'grey',
          modelLabel: '未指定模型', capabilities: [], organizationLabel: '组织状态未确认',
          ownerLabel: '我', host: '本机', availability: 'unknown',
          model: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
          channel: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
        } as never}
        extension={getAgentExtension(undefined, 'worker')}
        organizations={[]}
        onNavigateConnections={() => {}}
      />,
    );
    const facts = document.querySelector('dl.agent-detail-facts') as HTMLElement;
    expect(within(facts).queryByTestId('agent-detail-harness-model')).toBeNull();
  });

  it('shows only the harness value when the model is absent', () => {
    const extension = getAgentExtension({ worker: { harness: 'claude' } }, 'worker');
    expect(extension.harness).toBe('claude');
    expect(extension.model).toBeUndefined();

    render(
      <AgentDetail
        agent={{
          id: 'worker', displayName: 'Worker', role: '测试', avatarTone: 'grey',
          modelLabel: '未指定模型', capabilities: [], organizationLabel: '组织状态未确认',
          ownerLabel: '我', host: '本机', availability: 'unknown',
          model: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
          channel: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
        } as never}
        extension={extension}
        organizations={[]}
        onNavigateConnections={() => {}}
      />,
    );
    const row = screen.getByTestId('overview-harness');
    expect(row.textContent).toBe('claude');
    expect(row.textContent).not.toContain('·');
  });

  it('shows only the model value when the harness is absent', () => {
    const extension = getAgentExtension({ worker: { model: 'claude-sonnet-4.6' } }, 'worker');
    expect(extension.harness).toBeUndefined();
    expect(extension.preferredHarness).toBeUndefined();
    expect(extension.model).toBe('claude-sonnet-4.6');

    render(
      <AgentDetail
        agent={{
          id: 'worker', displayName: 'Worker', role: '测试', avatarTone: 'grey',
          modelLabel: '未指定模型', capabilities: [], organizationLabel: '组织状态未确认',
          ownerLabel: '我', host: '本机', availability: 'unknown',
          model: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
          channel: { kind: 'unknown', label: '状态未确认', sourceLabel: '状态未确认' },
        } as never}
        extension={extension}
        organizations={[]}
        onNavigateConnections={() => {}}
      />,
    );
    const row = screen.getByTestId('overview-model');
    expect(row.textContent).toBe('claude-sonnet-4.6');
    expect(row.textContent).not.toContain('·');
  });
});

describe('start-chat availability follows the navigation callback', () => {
  const baseAgent = {
    id: 'worker', displayName: 'Worker', role: '测试', avatarTone: 'grey' as const,
    modelServiceId: null, channelServiceId: null, organizationId: null,
    modelLabel: '未指定模型', capabilities: [], organizationLabel: '组织状态未确认',
    ownerLabel: '我', host: '本机', availability: 'unknown' as const,
    model: { kind: 'unknown' as const, label: '状态未确认', sourceLabel: '状态未确认' },
    channel: { kind: 'unknown' as const, label: '状态未确认', sourceLabel: '状态未确认' },
  };

  it('enables 开始对话 when the caller provides the navigation callback', () => {
    render(
      <AgentDetail
        agent={baseAgent}
        extension={getAgentExtension(undefined, 'worker')}
        organizations={[]}
        onNavigateConnections={() => {}}
        onStartChat={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: '开始对话' })).toBeEnabled();
  });

  it('renders no 开始对话 without a target callback', () => {
    render(
      <AgentDetail
        agent={baseAgent}
        extension={getAgentExtension(undefined, 'worker')}
        organizations={[]}
        onNavigateConnections={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: '开始对话' })).toBeNull();
  });
});


it('unlocks configuration when readback settles an unknown startup and retains the refusal on remount', async () => {
  const user = userEvent.setup();
  const action = vi.fn(async () => ({ tone: 'error' as const, text: '结果未知', dispatched: true, awaitingObservation: true }));
  const props = { agent: AGENTS_DETAIL_AVAILABLE, organizations: [], onNavigateConnections: vi.fn(),
    onSave: async () => ({ kind: 'failed' as const, message: 'unused' }),
    onDelete: async () => ({ kind: 'failed' as const, message: 'unused' }),
    onRuntimeAction: action, onRefreshRuntime: vi.fn(),
    extension: extensionWithGrantStates([], { runtime: 'unknown', preferredHarness: 'claude' }),
  };
  const view = render(<AgentDetail {...props} />);
  await user.click(screen.getByRole('button', { name: '启动' }));
  await user.click(screen.getByRole('button', { name: '保存并启动' }));
  expect(await screen.findByRole('button', { name: '刷新状态' })).toBeVisible();
  const row = { actor: AGENTS_DETAIL_AVAILABLE.id, uri: AGENTS_DETAIL_AVAILABLE.id,
    startupFailure: { operationId: 'failed-start', message: '启动失败：未找到所选模型，请修改模型配置后重新启动。' } } as never;
  view.rerender(<AgentDetail {...props} directoryRows={[row]} />);
  expect(screen.getByRole('button', { name: '启动' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '设置模型…' })).toBeEnabled();
  expect(screen.queryByText('已读取信息中暂无异常。')).toBeNull();
  expect(screen.getByRole('alert')).toHaveTextContent('未找到所选模型');
  view.unmount();
  render(<AgentDetail {...props} directoryRows={[row]} />);
  expect(screen.getByRole('alert')).toHaveTextContent('未找到所选模型');
  expect(action).toHaveBeenCalledTimes(1);
});
