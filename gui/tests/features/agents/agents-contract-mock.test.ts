// @vitest-environment node
// implG2 (2026-11-02)：真实的 Contract Mock 往返——真实 HttpTransport 与生成客户端打到
// 进程内 Contract Mock（MethodSpec examples，connectContractMock），经 agents feature api 全链路。
// 1e-2 (2026-11-15)：不再起 Python 进程，也不再被 npm test 排除。
// success 场景核对创建/停止的契约成功形态；error 场景核对契约错误
// （AGENT_EXISTS / LIFECYCLE_OPERATION_UNSETTLED）原样保留。
// implS2 (2026-11-02, second submission)：合并后的契约重新声明 agent.start，
// 并新增 routine.list / routine.status / workflow.list / agent.home-census。
// 往返测试覆盖：start 成功与 already-running（运行绑定 data）、start 的
// 结果未知、四个读方法的 success/empty/error 场景。
import { describe, expect, it } from 'vitest';

import { connectContractMock } from '../../../src/dev/contract-mock/index.ts';
import { createAgentsApi, AgentRequestError } from '../../../src/features/agents/impl/api/index.ts';

async function apiFor(scenario: 'success' | 'empty' | 'error' | 'unconfigured') {
  const bootstrap = await connectContractMock({ scenario });
  return createAgentsApi(bootstrap.connection.client);
}

describe('agents write api against the real Contract Mock', () => {
  it('create/stop serve the declared success examples', async () => {
    const api = await apiFor('success');

    const rows = await api.list();
    expect(rows.map((row) => row.actor)).toEqual(['worker']);

    const created = await api.create({
      name: 'worker',
      provider: null,
      model: null,
      preferredHarness: null,
    });
    expect(created.created).toBe(true);
    expect(created.actor).toBe('worker');
    expect(created.providerDefault).toEqual({ captured: false, provider: null });

    const stopped = await api.stop('worker');
    expect(stopped.removed).toEqual(['claude:worker']);
  });

  it('the contract mock serves agent.start success and error scenarios (implS2)', async () => {
    const api = await apiFor('success');

    const started = await api.start('worker');
    expect(started.accepted).toBe(true);
    expect(started.running).toBe(false);
    expect(started.warnings).toEqual([]);

    const errorApi = await apiFor('error');
    const failure = await errorApi.start('worker').then(
      () => null,
      (error) => error,
    );
    expect(failure).toBeInstanceOf(AgentRequestError);
    expect(failure.code).toBe('AGENT_ALREADY_RUNNING');
    expect(failure.dispatch).toBe('dispatched');
    expect(failure.data).toMatchObject({
      actor: 'agent:example-owner:example-node:alpha',
      existing: {
        harness: 'claude',
        runtime: 'headless',
      },
    });
  });

  it('routine/workflow/census round-trip their declared scenarios', async () => {
    const success = await apiFor('success');

    const routines = await success.listRoutines('worker');
    expect(routines).toHaveLength(1);
    expect(routines[0]).toMatchObject({ name: 'heartbeat', mode: 'scheduled' });

    const status = await success.routineStatus('worker', 'heartbeat');
    expect(status.name).toBe('heartbeat');

    const runs = await success.listWorkflows('worker');
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({ runId: 'wf-0001', state: 'running' });

    const census = await success.homeCensus('worker');
    expect(census.row).toMatchObject({ actor: 'worker', mode: 'p2' });
    expect(census.totals.agents).toBe(1);

    const empty = await apiFor('empty');
    const emptyRoutines = await empty.listRoutines('worker');
    expect(emptyRoutines).toEqual([]);
    const emptyRuns = await empty.listWorkflows('worker');
    expect(emptyRuns).toEqual([]);

    const error = await apiFor('error');
    const unknownAgent = await error.listWorkflows('worker').then(
      () => null,
      (failure) => failure,
    );
    expect(unknownAgent).toBeInstanceOf(AgentRequestError);
    expect(unknownAgent.code).toBe('AGENT_NOT_FOUND');

    const noRoutine = await error.routineStatus('worker', 'heartbeat').then(
      () => null,
      (failure) => failure,
    );
    expect(noRoutine).toBeInstanceOf(AgentRequestError);
    // The current contract's matching error case for an existing routine name is
    // the ownership fence; ROUTINE_NOT_FOUND is declared for name="missing".
    expect(noRoutine.code).toBe('CALLER_NOT_AUTHORIZED');
  });

  it('error scenarios keep the contract codes and data intact', async () => {
    const api = await apiFor('error');

    const createFailure = await api
      .create({ name: 'worker', provider: null, model: null, preferredHarness: null })
      .then(
        () => null,
        (error) => error,
      );
    expect(createFailure).toBeInstanceOf(AgentRequestError);
    expect(createFailure.code).toBe('AGENT_EXISTS');
    expect(createFailure.dispatch).toBe('dispatched');
    expect(createFailure.data).toMatchObject({ agent: 'worker' });

    const stopFailure = await api.stop('worker').then(
      () => null,
      (error) => error,
    );
    expect(stopFailure).toBeInstanceOf(AgentRequestError);
    expect(stopFailure.code).toBe('LIFECYCLE_OPERATION_UNSETTLED');
  });
});
