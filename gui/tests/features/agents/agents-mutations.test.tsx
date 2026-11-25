import { createTestAgentsApi as createAgentsApi } from '../../unified-backend/agents-test-client';
/**
 * implG2 (2026-11-02)：创建/停止走生成 client 的行为测试。
 * 数据全部来自 MethodSpec examples 派生的 fixtures；写操作 retry=never，
 * 断言不自动重试、unknown 显示结果未知。
 * implG4 (2026-11-02)：四页签详情落地，管理面板并入页签；运行控制迁移为
 * 详情头部的启动/停止。
 * 2026-11-14：契约网关不定义 update/delete，没有对应控件；被目录拒绝的调用
 * 以 refused 结果或 outcome.refusal 返回，由页面发出通知。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { contractExamples } from '../../../src/dev/contract-mock';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import {
  AgentRequestError,
  createContractAgentGateway,
  createRuntimeActionHandler,
  type AgentsApi,
} from '../../../src/features/agents/impl/api';
import { authenticatedTestActions } from '../../support/demo-baseline';

import { AgentDetail } from '../../../src/features/agents/impl/AgentDetail';
import { getAgentExtension } from '../../../src/features/agents/impl/agent-extensions';
import {
  AGENTS_LIST_ROWS_DATA,
  type AgentsListRowFixture,
} from '../../fixtures/agents/agent-list';
import {
  CREATE_NEW_AGENT,
  EXAMPLE_AGENT_NAME,
  STOP_RUNNING_AGENT,
  STOP_UNSETTLED,
} from '../../fixtures/agents/agent-mutations';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function domainError(
  code: string,
  message: string,
  dispatch: 'not-dispatched' | 'dispatched' | 'unknown' = 'dispatched',
  data?: unknown,
) {
  return new AgentRequestError(code, message, dispatch, data, true);
}

interface WriteOptions {
  readonly rows?: readonly AgentsListRowFixture[];
  readonly createResult?: Record<string, unknown>;
  readonly createError?: Error;
  readonly destroyError?: Error;
  readonly stopResult?: Record<string, unknown>;
  readonly stopError?: Error;
  readonly startResult?: Record<string, unknown>;
  readonly startError?: unknown;
}

function fixtureApiWithWrites(options: WriteOptions = {}) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  let listReads = 0;
  const removed = new Set<string>();
  let previewName = "";
  const createdRows: Record<string, unknown>[] = [];
  const api = createAgentsApi({
    async request(method, params) {
      calls.push({ method, params });
      if (method === 'agent.list') {
        listReads += 1;
        return {
          ok: true,
          agents: [...(options.rows ?? AGENTS_LIST_ROWS_DATA), ...createdRows].filter(row => !removed.has(String(row.actor))),
          agentsWithoutRoutine: [],
        };
      }
      if (method === 'agent.destroy.preview') {
        previewName = String(params.name);
        return contractExamples('agent.destroy.preview').find(row => row.name === 'ready')!.result;
      }
      if (method === 'agent.destroy') {
        if (options.destroyError) throw options.destroyError;
        removed.add(previewName);
        return contractExamples('agent.destroy').find(row => row.name === 'deleted')!.result;
      }
      if (method === 'agent.secret.list') {
        return { ok: true, grants: [] };
      }
      if (method === 'agent.provider.default') {
        return { ok: true, provider: 'claude', previous: null };
      }
      if (method === 'agent.provider.status') {
        return {
          ok: true,
          actor: 'agent:owner:node:a.squire',
          agent: 'a.squire',
          agentId: 'a.squire',
          grants: [],
        };
      }
      if (method === 'agent.create') {
        if (options.createError) throw options.createError;
        const result = options.createResult ?? CREATE_NEW_AGENT.result;
        if (result?.created === true && result.agent) createdRows.push(result.agent as Record<string, unknown>);
        return result;
      }
      if (method === 'agent.provider.allow') {
        return {
          ok: true,
          changed: true,
          grant: { provider: params.provider, grantId: params.provider },
          restart: { state: 'not-supported' },
        };
      }
      if (method === 'agent.stop') {
        if (options.stopError) throw options.stopError;
        return options.stopResult ?? STOP_RUNNING_AGENT.result;
      }
      if (method === 'agent.start') {
        if (options.startError) throw options.startError;
        return options.startResult ?? { ok: true, accepted: true, running: true };
      }
      if (method === 'routine.list') {
        return { ok: true, routines: [] };
      }
      if (method === 'routine.status') {
        throw new Error('unexpected method routine.status');
      }
      if (method === 'workflow.list') {
        return { ok: true, runs: [] };
      }
      if (method === 'agent.home-census') {
        return {
          ok: true,
          readAtMs: 0,
          registry: { path: '/tmp/x', version: 1 },
          agents: [],
          totals: { agents: 0, byModeHarness: [], homeUnprovisioned: 0 },
        };
      }
      throw new Error(`unexpected method ${method}`);
    },
  });
  return {
    api,
    calls,
    listReads: () => listReads,
  };
}

function renderDirectoryApp(api: AgentsApi) {
  const user = userEvent.setup();
  render(
    <FixtureApp
      dataMode="fixture"
      capabilities={{
        firstRun: authenticatedTestActions(),
        agents: api,
        sharedCredentials: {
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
      }}
    />,
  );
  return { user };
}

async function openAgents(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole('button', { name: 'Agent' })[0]);
}

describe('contract mutation gateway (create wired; no update or delete)', () => {
  it('create returns the created view, fires reload, and never invents fields', async () => {
    const { api, calls } = fixtureApiWithWrites();
    let reloads = 0;
    const gateway = createContractAgentGateway({
      api,
      afterCreate: () => {
        reloads += 1;
      },
    });

    const result = await gateway.create({
      displayName: EXAMPLE_AGENT_NAME,
      role: 'worker',
      modelServiceId: '',
      organizationId: 'group-team',
      provider: 'claude',
      model: 'claude-sonnet',
      preferredHarness: 'pi',
    });

    expect(result.kind).toBe('created');
    if (result.kind === 'created') {
      expect(result.agent.id).toBe('worker');
      expect(result.notice).toBeUndefined();
    }
    expect(calls[0]).toEqual({
      method: 'agent.create',
      params: {
        name: EXAMPLE_AGENT_NAME,
        provider: 'claude',
        model: 'claude-sonnet',
        preferredHarness: 'pi',
      },
    });
    expect(calls).toHaveLength(1);
    expect(reloads).toBe(1);
  });

  it('create maps AGENT_EXISTS to a failed result with the contract wording', async () => {
    const { api } = fixtureApiWithWrites({
      createError: domainError('AGENT_EXISTS', 'taken', 'dispatched', {
        agent: EXAMPLE_AGENT_NAME,
      }),
    });
    const gateway = createContractAgentGateway({ api });

    const result = await gateway.create({
      displayName: EXAMPLE_AGENT_NAME,
      role: 'worker',
      modelServiceId: '',
      organizationId: null,
    });

    expect(result.kind).toBe('failed');
    if (result.kind === 'failed') {
      expect(result.message).toContain('已被占用');
    }
  });

  it('create with unknown dispatch reports 结果未知 and never success', async () => {
    const { api } = fixtureApiWithWrites({
      createError: domainError('DAEMON_ERROR', 'lost', 'unknown'),
    });
    const gateway = createContractAgentGateway({ api });

    const result = await gateway.create({
      displayName: EXAMPLE_AGENT_NAME,
      role: 'worker',
      modelServiceId: '',
      organizationId: null,
    });

    expect(result.kind).toBe('unknown');
    if (result.kind === 'unknown') {
      expect(result.message).toContain('结果未知');
      expect(result.message).not.toContain('成功');
    }
  });

  it('defines no update or delete, so no edit or delete control exists for it', async () => {
    const { api } = fixtureApiWithWrites();
    const gateway = createContractAgentGateway({ api });

    expect(gateway.update).toBeUndefined();
    expect(gateway.delete).toBeUndefined();
  });

  it('create refused by the catalog resolves as a machine refusal and reloads nothing', async () => {
    let reloads = 0;
    const gateway = createContractAgentGateway({
      api: createAgentsApi({ request: async () => { throw new Error('must not call any method'); } },
        (method) => (method === 'agent.create'
          ? { available: false, code: 'METHOD_NOT_CALLABLE', reason: '目录声明 agent.create 不可调用。' }
          : { available: true })),
      afterCreate: () => { reloads += 1; },
    });

    const result = await gateway.create({ displayName: 'worker', role: '', modelServiceId: '', organizationId: null });

    expect(result).toEqual({ kind: 'refused', refusal: { code: 'METHOD_NOT_CALLABLE', source: 'agent.create' } });
    expect(reloads).toBe(0);
    expect(JSON.stringify(result)).not.toContain('目录声明');
  });
});

describe('runtime action handler (start dispatch honesty, stop honesty)', () => {
  it('start dispatches agent.start once and reports the accepted outcome', async () => {
    const { api, calls } = fixtureApiWithWrites({
      startResult: { ok: true, accepted: true, running: false },
    });
    let settled = 0;
    const handler = createRuntimeActionHandler(api, () => {
      settled += 1;
    });

    const outcome = await handler('start', EXAMPLE_AGENT_NAME);

    expect(outcome.tone).toBe('status');
    expect(outcome.text).toContain('启动已受理');
    expect(outcome.text).toContain('尚未观察到运行状态');
    expect(outcome.dispatched).toBe(true);
    expect(settled).toBe(1);
    expect(calls).toEqual([
      { method: 'agent.start', params: { name: EXAMPLE_AGENT_NAME } },
    ]);
  });

  it('AGENT_ALREADY_RUNNING keeps the running binding in the outcome', async () => {
    const { api, calls } = fixtureApiWithWrites({
      startError: domainError(
        'AGENT_ALREADY_RUNNING',
        'agent is already being served',
        'dispatched',
        {
          actor: 'agent:acme:node-1:worker',
          existing: {
            actor: 'agent:acme:node-1:worker',
            harness: 'claude',
            runtime: 'headless',
            sessionId: 'sess-1',
            boundAtMs: 1793604000000,
          },
          requestedHarness: 'claude',
          requestedRuntime: 'headless',
        },
      ),
    });
    const handler = createRuntimeActionHandler(api, () => {});

    const outcome = await handler('start', EXAMPLE_AGENT_NAME);

    expect(outcome.tone).toBe('error');
    expect(outcome.text).toContain('已在运行');
    expect(outcome.dispatched).toBe(true);
    expect(outcome.runningBinding).toMatchObject({
      actor: 'agent:acme:node-1:worker',
      harness: 'claude',
      runtime: 'headless',
      sessionId: 'sess-1',
    });
    expect(calls).toHaveLength(1);
  });

  it('unknown start outcome reports 结果未知, refreshes, never retries', async () => {
    const { api, calls } = fixtureApiWithWrites({
      startError: domainError(
        'SUBMIT_OUTCOME_UNKNOWN',
        'lifecycle reservation outcome is unknown: lifecycle-down:example',
        'dispatched',
        { operationId: 'lifecycle-start:example' },
      ),
    });
    let settled = 0;
    const handler = createRuntimeActionHandler(api, () => {
      settled += 1;
    });

    const outcome = await handler('start', EXAMPLE_AGENT_NAME);

    expect(outcome.tone).toBe('error');
    expect(outcome.text).toContain('启动受理结果未知');
    expect(outcome.text).toContain('lifecycle-start:example');
    expect(outcome.dispatched).toBe(true);
    expect(settled).toBe(1);
    expect(calls).toHaveLength(1);
  });

  it('a not-dispatched start refusal does not refresh the directory', async () => {
    const { api, calls } = fixtureApiWithWrites({
      startError: domainError(
        'METHOD_NOT_FOUND',
        'method not served',
        'not-dispatched',
      ),
    });
    let settled = 0;
    const handler = createRuntimeActionHandler(api, () => {
      settled += 1;
    });

    const outcome = await handler('start', EXAMPLE_AGENT_NAME);

    expect(outcome.dispatched).toBe(false);
    expect(settled).toBe(0);
    expect(calls).toHaveLength(1);
  });

  it('stop success reports the removal, empty removal stays a normal status', async () => {
    const removed = fixtureApiWithWrites();
    const idle = fixtureApiWithWrites({
      stopResult: { ok: true, removed: [] },
    });
    const handlerRemoved = createRuntimeActionHandler(removed.api, () => {});
    const handlerIdle = createRuntimeActionHandler(idle.api, () => {});

    const done = await handlerRemoved('stop', EXAMPLE_AGENT_NAME);
    expect(done.tone).toBe('status');
    expect(done.text).toContain('停止已完成');
    expect(done.awaitingObservation).toBe(false);

    const nothing = await handlerIdle('stop', EXAMPLE_AGENT_NAME);
    expect(nothing.tone).toBe('status');
    expect(nothing.text).toContain('没有需要停止');
    expect(nothing.awaitingObservation).toBe(false);
  });

  it.each(['LIFECYCLE_OPERATION_UNSETTLED', 'SUBMIT_OUTCOME_UNKNOWN', 'DOMAIN_COMMAND_TIMEOUT'])('unsettled stop %s waits for observation and refreshes the directory', async code => {
    const { api } = fixtureApiWithWrites({
      stopError: domainError(
        code,
        STOP_UNSETTLED.error?.message ?? 'wait elapsed',
      ),
    });
    let settled = 0;
    const handler = createRuntimeActionHandler(api, () => {
      settled += 1;
    });

    const outcome = await handler('stop', EXAMPLE_AGENT_NAME);

    expect(outcome.tone).toBe('error');
    expect(outcome.text).toContain('结果未知');
    expect(outcome.dispatched).toBe(true);
    expect(outcome.awaitingObservation).toBe(true);
    expect(settled).toBe(1);
  });

  it('a not-dispatched refusal does not claim a refresh', async () => {
    const { api } = fixtureApiWithWrites({
      createError: domainError('INVALID_ARGUMENT', 'bad params', 'not-dispatched'),
    });
    let reloads = 0;
    const gateway = createContractAgentGateway({
      api,
      afterCreate: () => {
        reloads += 1;
      },
    });

    const result = await gateway.create({
      displayName: '',
      role: '',
      modelServiceId: '',
      organizationId: null,
    });
    expect(result.kind).toBe('failed');
    if (result.kind === 'failed') {
      expect(result.message).toContain('未发送');
    }
    expect(reloads).toBe(0);
  });
});

describe('AgentDetail runtime controls (implG4 tabs)', () => {
  function detailAgentView() {
    return {
      id: 'worker',
      displayName: 'worker',
      role: '',
      avatarTone: 'green' as const,
      modelServiceId: null,
      channelServiceId: null,
      organizationId: null,
      modelLabel: '未指定模型',
      capabilities: [],
      organizationLabel: '组织状态未确认',
      ownerLabel: '我',
      host: '本机',
      availability: 'unknown' as const,
      model: { kind: 'unknown' as const, label: '状态未确认', sourceLabel: '状态未确认' },
      channel: { kind: 'unknown' as const, label: '状态未确认', sourceLabel: '状态未确认' },
    };
  }

  it('hides the page-level stop action when no runtime handler is wired', () => {
    renderDetail();
    expect(screen.queryByRole('button', { name: '停止' })).toBeNull();
  });

  function renderDetail() {
    const user = userEvent.setup();
    render(
      <AgentDetail
        agent={detailAgentView()}
        extension={getAgentExtension(undefined, 'worker')}
        organizations={[]}
        onNavigateConnections={() => {}}
      />,
    );
    return { user };
  }



  it('with a wired handler stop shows the removal result', async () => {
    const { api } = fixtureApiWithWrites();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await user.click(await screen.findByTestId('contact-squire'));

    await user.click(await screen.findByRole('button', { name: '停止' }));
    expect(
      await screen.findByText('停止已完成，已移除 1 个运行项。'),
    ).toBeTruthy();
  });
});

describe('agents page create/stop wiring (implG2)', () => {
  it('create opens the new Agent configuration after reloading the directory', async () => {
    const { api, calls, listReads } = fixtureApiWithWrites();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await screen.findByTestId('contact-squire');
    expect(listReads()).toBe(2);

    const createButtons = screen.getAllByRole('button', { name: '创建 Agent' });
    expect(createButtons[0]).toBeEnabled();
    await user.click(createButtons[0]);

    await user.type(await screen.findByLabelText('名称'), EXAMPLE_AGENT_NAME);
    fireEvent.click(screen.getByLabelText('harness'));
    fireEvent.click(await screen.findByRole('option', { name: 'claude' }));
    await user.click(screen.getAllByRole('button', { name: '创建 Agent' }).at(-1)!);

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.method === 'agent.create' &&
            call.params.name === EXAMPLE_AGENT_NAME,
        ),
      ).toBe(true);
    });
    await screen.findByTestId('agent-detail-panel');
    expect(screen.queryByText('Agent 创建已完成，Key 已授权。')).toBeNull();
    expect(calls.some(call => call.method === 'agent.start')).toBe(false);
    expect(listReads()).toBeGreaterThanOrEqual(3);
  });

  it('blocks create without a harness in directory mode', async () => {
    const { api, calls } = fixtureApiWithWrites();
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await screen.findByTestId('contact-squire');

    await user.click(screen.getAllByRole('button', { name: '创建 Agent' })[0]);
    await user.type(await screen.findByLabelText('名称'), 'no-harness');
    await user.click(screen.getAllByRole('button', { name: '创建 Agent' }).at(-1)!);

    expect(screen.getByText('请选择 harness')).toBeTruthy();
    expect(calls.some((call) => call.method === 'agent.create')).toBe(false);
  });

  it('a taken name keeps the form open with the contract wording', async () => {
    const { api } = fixtureApiWithWrites({
      createError: domainError('AGENT_EXISTS', 'taken', 'dispatched', {
        agent: EXAMPLE_AGENT_NAME,
      }),
    });
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await screen.findByTestId('contact-squire');

    await user.click(screen.getAllByRole('button', { name: '创建 Agent' })[0]);
    await user.type(await screen.findByLabelText('名称'), EXAMPLE_AGENT_NAME);
    fireEvent.click(screen.getByLabelText('harness'));
    fireEvent.click(await screen.findByRole('option', { name: 'claude' }));
    await user.click(screen.getAllByRole('button', { name: '创建 Agent' }).at(-1)!);

    const alert = await screen.findByText(/已被占用/);
    expect(alert).toBeTruthy();
  });


  it('hides deletion without a confirmation capability', async () => {
    const { api } = fixtureApiWithWrites();
    const optional: AgentsApi = api;
    optional.previewDestroy = undefined;
    optional.destroy = undefined;
    const { user } = renderDirectoryApp(api);
    await openAgents(user);
    await user.click(await screen.findByTestId('contact-squire'));

    const detail = await screen.findByTestId('agent-detail-panel');
    await user.click(within(detail).getByRole('tab', { name: '凭据' }));
    expect(await within(detail).findByText('无 Key 授权。')).toBeTruthy();
    expect(within(detail).queryByRole('button', { name: '删除' })).toBeNull();
    expect(screen.queryByRole('button', { name: '确认删除' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/agent\.destroy|暂不可用/);
  });
});


it('previews and confirms through the generated client, then reloads the directory', async () => {
  const { api, calls } = fixtureApiWithWrites();
  const { user } = renderDirectoryApp(api);
  await openAgents(user);
  await user.click(await screen.findByTestId('contact-squire'));
  const remove = within(await screen.findByTestId('agent-detail-panel')).getByRole('button', { name: '删除' });
  await user.click(remove);
  let dialog = screen.getByRole('dialog', { name: '删除 Agent？' });
  await user.click(within(dialog).getByRole('button', { name: '取消' }));
  expect(calls.filter(row => row.method === 'agent.destroy')).toHaveLength(0);
  expect(remove).toHaveFocus();
  await user.click(remove);
  dialog = screen.getByRole('dialog', { name: '删除 Agent？' });
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '确认删除' })).toBeEnabled());
  await user.click(within(dialog).getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(screen.queryByTestId('contact-squire')).toBeNull());
  expect(calls.filter(row => row.method === 'agent.destroy')).toEqual([
    { method: 'agent.destroy', params: { operationId: 'example-destroy-operation' } },
  ]);
});

it('unknown deletion refreshes without repeating the write or removing the Agent', async () => {
  const { api, calls } = fixtureApiWithWrites({ destroyError: domainError('SUBMIT_OUTCOME_UNKNOWN', 'unknown', 'unknown') });
  const { user } = renderDirectoryApp(api);
  await openAgents(user);
  await user.click(await screen.findByTestId('contact-squire'));
  await user.click(screen.getByRole('button', { name: '删除' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  expect(await screen.findByText(/删除结果未确认/)).toBeInTheDocument();
  expect(screen.getByTestId('agent-detail-panel')).toBeInTheDocument();
  expect(calls.filter(row => row.method === 'agent.destroy')).toHaveLength(1);
});
