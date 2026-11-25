import { createTestAgentsApi as createAgentsApi } from '../../unified-backend/agents-test-client';
import { describe, expect, it } from 'vitest';

import {
  AgentRequestError,
  agentFailureMessage,
  directoryRowToExtension,
  directoryRowToView,
  grantRowsToProviderGrants,
} from '../../../src/features/agents/impl/api';
import { DomainError, TransportError } from '../../../src/client';
import {
  AGENTS_LIST_ROW_NO_RUNSTATE,
  AGENTS_LIST_ROWS_DATA,
  AGENTS_LIST_ROWS_EMPTY,
  AGENTS_SECRET_GRANTS_SQUIRE,
} from '../../fixtures/agents/agent-list';

function portWith(rows: readonly unknown[], grants: readonly unknown[] = []) {
  const calls: string[] = [];
  return {
    calls,
    port: {
      async request(method: string, _params: Record<string, unknown>) {
        calls.push(method);
        if (method === 'agent.list') {
          return { ok: true, agents: rows, agentsWithoutRoutine: [] };
        }
        if (method === 'agent.secret.list') {
          return { ok: true, grants };
        }
        if (method === 'agent.provider.default') {
          return { ok: true, provider: 'claude', previous: null };
        }
        throw new Error(`unexpected method ${method}`);
      },
    },
  };
}

const DIAGNOSTICS_BASE = {
  requestId: 'req-test',
  status: 200,
  catalogDigest: null,
  attempts: 1,
  startedAt: 0,
  durationMs: 1,
};

function domainError(
  code: string,
  message: string,
  dispatch: 'not-dispatched' | 'dispatched' | 'unknown' = 'dispatched',
  data?: unknown,
) {
  return new DomainError(code, message, data, {
    method: 'agent.test',
    ...DIAGNOSTICS_BASE,
    dispatch,
  });
}

function transportError(
  code: string,
  message: string,
  dispatch: 'not-dispatched' | 'dispatched' | 'unknown' = 'unknown',
) {
  return new TransportError('network', code, message, {
    method: 'agent.test',
    ...DIAGNOSTICS_BASE,
    dispatch,
  });
}

describe('agents read api mapping', () => {
  it('does not turn runtime metadata or raw capability names into product badges', async () => {
    const { port } = portWith([{ ...AGENTS_LIST_ROWS_DATA[0], capabilities: {
      harness: 'pi', model: 'deepseek-flash', provider: 'deepseek', tier: 'high',
      updated_at_ms: 1794758192554, proactive_send: true, tool_injection: true,
    } }]);
    expect((await createAgentsApi(port).list())[0].capabilities).toEqual([]);
  });

  it('maps agent.list rows through the generated client only', async () => {
    const { port, calls } = portWith(AGENTS_LIST_ROWS_DATA);
    const api = createAgentsApi(port);

    const rows = await api.list();

    expect(calls).toEqual(['agent.list']);
    expect(rows.map((row) => row.actor)).toEqual([
      'squire',
      'researcher',
      'writer',
      'archivist',
    ]);
    expect(rows[0]).toMatchObject({
      displayName: 'squire',
      owner: '未提供',
      host: '未提供',
      provider: 'claude',
      harness: 'pi',
    });
    expect(rows[2]).toMatchObject({ owner: 'alice', host: 'home-1' });
  });

  it('labels a renamed agent by displayName while keying it on the immutable actor', async () => {
    const renamed = { ...AGENTS_LIST_ROWS_DATA[1],
      agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa', displayName: 'reviewer' };
    const blank = { ...AGENTS_LIST_ROWS_DATA[2], displayName: '  ' };
    const { port } = portWith([renamed, blank, { ...AGENTS_LIST_ROWS_DATA[3], displayName: null }]);

    const rows = await createAgentsApi(port).list();

    expect(rows.map((row) => [row.actor, row.displayName])).toEqual([
      ['researcher', 'reviewer'],
      ['writer', 'writer'],
      ['archivist', 'archivist'],
    ]);
    expect(directoryRowToView(rows[0])).toMatchObject({
      id: 'researcher',
      agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa',
      displayName: 'reviewer',
    });
  });

  it('returns an empty list without inventing rows', async () => {
    const { port } = portWith(AGENTS_LIST_ROWS_EMPTY);
    const api = createAgentsApi(port);

    await expect(api.list()).resolves.toEqual([]);
  });

  it('maps run state honestly: observed shows, inferred marks, incomplete/stale stays unknown', async () => {
    const { port } = portWith(AGENTS_LIST_ROWS_DATA);
    const api = createAgentsApi(port);
    const rows = await api.list();

    expect(rows[0].runState).toEqual({ runtime: 'online', runtimeDetail: '空闲', summaryState: 'normal' });
    expect(rows[1].runState).toEqual({ runtime: 'offline', runtimeDetail: '未运行', summaryState: 'abnormal' });
    expect(rows[2].runState).toEqual({
      runtime: 'unknown',
      runtimeDetail: '运行信息不完整',
    });
    expect(rows[3].runState).toEqual({
      runtime: 'unknown',
      runtimeDetail: '运行信息已过期',
    });

    const { port: inferredPort } = portWith([{
      ...AGENTS_LIST_ROWS_DATA[0],
      actor: 'inferred',
      runState: { state: 'in-turn', certainty: 'inferred', observationHealth: 'current' },
    }]);
    const inferred = await createAgentsApi(inferredPort).list();
    expect(inferred[0].runState).toEqual({
      runtime: 'online',
      runtimeDetail: '进行中（推断）',
    });
  });

  it('maps a row without runState to unknown, never online', async () => {
    const { port } = portWith(AGENTS_LIST_ROW_NO_RUNSTATE);
    const api = createAgentsApi(port);
    const rows = await api.list();

    expect(rows[0].runState).toEqual({ runtime: 'unknown' });
  });

  it('maps directory rows to the shared agent view', () => {
    const { port } = portWith(AGENTS_LIST_ROWS_DATA);
    const api = createAgentsApi(port);

    return api.list().then((rows) => {
      const view = directoryRowToView(rows[0]);
      expect(view).toMatchObject({
        id: 'squire',
        displayName: 'squire',
        ownerLabel: '未提供',
        host: '未提供',
        modelLabel: 'claude · claude-sonnet',
        availability: 'available',
        organizationLabel: '组织状态未确认',
      });
      expect(view.model).toEqual({
        kind: 'unknown',
        label: '状态未确认',
        sourceLabel: '状态未确认',
      });
      const network = directoryRowToView(rows[2]);
      expect(network).toMatchObject({ ownerLabel: 'alice', host: 'home-1' });
    });
  });

  it('maps rows to manage-panel extensions without claiming runtime facts', () => {
    const { port } = portWith(AGENTS_LIST_ROWS_DATA);
    const api = createAgentsApi(port);

    return api.list().then((rows) => {
      const extension = directoryRowToExtension(rows[0]);
      expect(extension).toMatchObject({
        runtime: 'online',
        runtimeDetail: '空闲',
        harness: 'pi',
        model: 'claude-sonnet',
        preferredHarness: 'pi',
        preset: false,
      });
      expect(extension.providerGrants).toEqual([]);
      expect(extension.capabilityGrants).toEqual([]);
      expect(directoryRowToExtension(rows[1]).runtime).toBe('offline');
    });
  });

  it('maps secret grants: user-provider only, agent-owned rows stay out', () => {
    const grants = grantRowsToProviderGrants(
      AGENTS_SECRET_GRANTS_SQUIRE.map((grant) => ({ ...grant, environmentNames: [...grant.environmentNames] })),
    );

    expect(grants).toHaveLength(1);
    expect(grants[0]).toEqual({
      provider: 'api_key',
      state: 'ok',
      source: 'user-granted',
      ownerLabel: 'allen',
      revision: 3,
    });
    expect(grantRowsToProviderGrants([])).toEqual([]);
  });

  it('does not turn arbitrary capability object keys into product badges', async () => {
    const { port } = portWith([
      {
        actor: 'researcher',
        owner: '',
        machine: 'lab-2',
        provider: null,
        model: null,
        harness: null,
        preferredHarness: null,
        capabilities: {
          资料检索: true,
          对比分析: true,
          长文撰写: false,
          archived: null,
        },
      },
      {
        actor: 'minimal',
        owner: '',
        machine: '',
        provider: null,
        model: null,
        harness: null,
        preferredHarness: null,
      },
    ]);
    const api = createAgentsApi(port);
    const rows = await api.list();

    expect(directoryRowToView(rows[0]).capabilities).toEqual([]);
    expect(directoryRowToView(rows[1]).capabilities).toEqual([]);
  });

  it('reports provider default with the response provider', async () => {
    const { port } = portWith(AGENTS_LIST_ROWS_EMPTY, AGENTS_SECRET_GRANTS_SQUIRE);
    const api = createAgentsApi(port);

    await expect(api.defaultProvider()).resolves.toEqual({
      ready: true,
      provider: 'claude',
    });
    await expect(api.listGrants('squire')).resolves.toHaveLength(2);
  });

  it('propagates read errors instead of empty success', async () => {
    const api = createAgentsApi({
      async request(method: string) {
        if (method === 'agent.list') {
          throw new Error('daemon unavailable');
        }
        throw new Error(`unexpected method ${method}`);
      },
    });

    await expect(api.list()).rejects.toThrow('daemon unavailable');
  });
});

describe('credential evidence honesty (G1-1)', () => {
  it('never derives connected/available from the grant inventory alone', () => {
    const { port } = portWith(AGENTS_LIST_ROWS_DATA, AGENTS_SECRET_GRANTS_SQUIRE);
    const api = createAgentsApi(port);

    return api.list().then((rows) => {
      const view = directoryRowToView(rows[1]);
      expect(view.model).toMatchObject({ kind: 'unknown', label: '状态未确认' });
      expect(view.model.label).not.toBe('已连接');
      expect(view.availability).not.toBe('available');
    });
  });
});


describe('model search corpus (G1-2)', () => {
  it('carries the actual model (not only provider) in the searchable label', () => {
    const { port } = portWith(AGENTS_LIST_ROWS_DATA);
    const api = createAgentsApi(port);

    return api.list().then((rows) => {
      const squire = directoryRowToView(rows[0]);
      expect(squire.modelLabel).toContain('claude-sonnet');
      expect(squire.modelLabel).toContain('claude');
      const bare = directoryRowToView(rows[1]);
      expect(bare.modelLabel).toBe('未指定模型');
    });
  });
});

describe('agents write api mapping (implG2)', () => {
  function createRowFixture() {
    return {
      actor: 'worker',
      agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa',
      owner: '我',
      machine: '本机',
      provider: 'claude',
      model: 'claude-sonnet',
      harness: null,
      preferredHarness: 'pi',
    };
  }

  function portWithWrites(handlers: {
    'agent.create'?: (params: Record<string, unknown>) => unknown;
    'agent.stop'?: (params: Record<string, unknown>) => unknown;
    'agent.start'?: (params: Record<string, unknown>) => unknown;
    'routine.list'?: (params: Record<string, unknown>) => unknown;
    'routine.status'?: (params: Record<string, unknown>) => unknown;
    'workflow.list'?: (params: Record<string, unknown>) => unknown;
    'agent.home-census'?: (params: Record<string, unknown>) => unknown;
    'agent.rename'?: (params: Record<string, unknown>) => unknown;
    'agent.secret.list'?: (params: Record<string, unknown>) => unknown;
    'agent.secret.grant'?: (params: Record<string, unknown>) => unknown;
    'agent.secret.revoke'?: (params: Record<string, unknown>) => unknown;
    'agent.provider.login'?: (params: Record<string, unknown>) => unknown;
    'agent.provider.logout'?: (params: Record<string, unknown>) => unknown;
  }) {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    return {
      calls,
      port: {
        async request(method: string, params: Record<string, unknown>) {
          calls.push({ method, params });
          const handler = handlers[method as keyof typeof handlers];
          if (!handler) throw new Error(`unexpected method ${method}`);
          const value = handler(params);
          if (value instanceof Error) throw value;
          return value;
        },
      },
    };
  }

  it('create maps the draft to declared params only and reports the created entity', async () => {
    const row = createRowFixture();
    const { port, calls } = portWithWrites({
      'agent.create': () => ({
        ok: true,
        created: true,
        agent: row,
        providerDefault: null,
      }),
    });
    const api = createAgentsApi(port);

    const outcome = await api.create({
      name: 'worker',
      org: 'group-team',
      provider: 'claude',
      model: 'claude-sonnet',
      preferredHarness: 'pi',
    });

    expect(calls).toEqual([
      {
        method: 'agent.create',
        params: {
          name: 'worker',
          provider: 'claude',
          model: 'claude-sonnet',
          preferredHarness: 'pi',
        },
      },
    ]);
    expect(outcome.created).toBe(true);
    expect(outcome.orgMembership).toBeUndefined();
    expect(outcome.actor).toBe('worker');
    expect(outcome.entity).toMatchObject({ actor: 'worker', preferredHarness: 'pi' });
    expect(outcome.providerDefault).toEqual({ captured: false, provider: null });
  });

  it('creates an Agent without an organization membership request', async () => {
    const { port, calls } = portWithWrites({
      'agent.create': () => ({ ok: true, created: true, agent: createRowFixture(), providerDefault: null }),
    });
    const outcome = await createAgentsApi(port).create({
      name: 'worker', org: 'group-team', provider: null, model: null, preferredHarness: null,
    });
    expect(outcome.created).toBe(true);
    expect(outcome.orgMembership).toBeUndefined();
    expect(calls.map((call) => call.method)).toEqual(['agent.create']);
  });

  it('create reports a captured provider default only for the object branch', async () => {
    const row = createRowFixture();
    const captured = portWithWrites({
      'agent.create': () => ({
        ok: true,
        created: false,
        agent: row,
        providerDefault: {
          provider: 'claude',
          credentialSource: 'setup-token',
          defaultCredential: {
            ownerActor: 'agent:acme:node-1:worker',
            ownerEntityToken: 'token',
            entryId: 'entry-0001',
            fieldName: 'OAUTH',
            kind: 'setup-token',
          },
        },
      }),
    });
    const historical = portWithWrites({
      'agent.create': () => ({
        ok: true,
        created: true,
        agent: row,
        providerDefault: 'claude',
      }),
    });
    const apiCaptured = createAgentsApi(captured.port);
    const apiHistorical = createAgentsApi(historical.port);

    const reused = await apiCaptured.create({ name: 'worker', provider: null, model: null, preferredHarness: null });
    expect(reused.created).toBe(false);
    expect(reused.providerDefault).toEqual({ captured: true, provider: 'claude' });

    const fromHistory = await apiHistorical.create({ name: 'worker', provider: null, model: null, preferredHarness: null });
    expect(fromHistory.created).toBe(true);
    expect(fromHistory.providerDefault).toEqual({ captured: false, provider: 'claude' });
  });

  it('create surfaces AGENT_EXISTS with the contract code and dispatched state', async () => {
    const { port } = portWithWrites({
      'agent.create': () =>
        domainError('AGENT_EXISTS', "the name 'worker' is already taken on this node", 'dispatched', {
          actor: 'agent:acme:node-1:worker',
          agent: 'worker',
        }),
    });
    const api = createAgentsApi(port);

    const failure = await api.create({ name: 'worker', provider: null, model: null, preferredHarness: null }).then(
      () => null,
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(AgentRequestError);
    const requestError = failure as InstanceType<typeof AgentRequestError>;
    expect(requestError.code).toBe('AGENT_EXISTS');
    expect(requestError.dispatch).toBe('dispatched');
    expect((requestError.data as { agent: string }).agent).toBe('worker');
  });

  it('start sends the declared params only and projects accepted/running/warnings', async () => {
    const { port, calls } = portWithWrites({
      'agent.start': () => ({
        ok: true,
        accepted: true,
        running: false,
        warnings: ['harness reported a slow launch'],
      }),
    });
    const api = createAgentsApi(port);

    const outcome = await api.start('worker');
    expect(outcome).toEqual({
      accepted: true,
      running: false,
      warnings: ['harness reported a slow launch'],
    });
    expect(calls).toEqual([
      { method: 'agent.start', params: { name: 'worker' } },
    ]);
  });

  it('start failures normalize into AgentRequestError with the contract code and data', async () => {
    const { port } = portWithWrites({
      'agent.start': () =>
        domainError(
          'AGENT_ALREADY_RUNNING',
          'agent is already being served',
          'dispatched',
          { existing: { actor: 'agent:acme:n1:worker' } },
        ),
    });
    const api = createAgentsApi(port);

    const failure = await api.start('worker').then(
      () => null,
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(AgentRequestError);
    expect((failure as AgentRequestError).code).toBe('AGENT_ALREADY_RUNNING');
    expect((failure as AgentRequestError).dispatch).toBe('dispatched');
  });

  it('routine.list projects the declared row fields and keeps missing values null', async () => {
    const { port, calls } = portWithWrites({
      'routine.list': () => ({
        ok: true,
        routines: [
          {
            name: 'heartbeat',
            owner: 'user:acme',
            actor: 'agent:acme:node-1:worker',
            actorOwnership: 'external',
            enabled: true,
            mode: 'scheduled',
            role: 'dispatch',
            nextDueMs: 1760000060000,
            lastOutcome: 'done',
            lastDispatchAtMs: 1760000000000,
            sourceErrorStreak: 0,
            missedPeriods: null,
            quarantined: false,
            quarantineReason: null,
            registrationId: null,
            readable: true,
            inFlight: [],
            launch: null,
          },
        ],
      }),
    });
    const api = createAgentsApi(port);

    const rows = await api.listRoutines('worker');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      name: 'heartbeat',
      mode: 'scheduled',
      enabled: true,
      nextDueMs: 1760000060000,
      lastOutcome: 'done',
      inFlightCount: 0,
      launch: null,
    });
    expect(calls).toEqual([
      { method: 'routine.list', params: { agent: 'worker' } },
    ]);
  });

  it('routine.status projects scheduleEvents without inventing fields', async () => {
    const { port } = portWithWrites({
      'routine.status': () => ({
        ok: true,
        name: 'heartbeat',
        owner: 'user:acme',
        actor: 'agent:acme:node-1:worker',
        actorOwnership: 'external',
        enabled: true,
        mode: 'scheduled',
        role: 'dispatch',
        nextDueMs: 1760000060000,
        lastOutcome: 'done',
        lastDispatchAtMs: 1760000000000,
        sourceErrorStreak: 1,
        missedPeriods: null,
        quarantined: false,
        quarantineReason: null,
        registrationId: 'reg-1',
        readable: true,
        inFlight: [],
        launch: null,
        outcomes: ['done'],
        scheduleEvents: [
          {
            routine: 'heartbeat',
            slot_ms: 1760000060000,
            disposition: 'dispatched',
            count: 1,
            reason: 'timer',
            recorded_at_ms: 1760000060005,
          },
        ],
      }),
    });
    const api = createAgentsApi(port);

    const status = await api.routineStatus('worker', 'heartbeat');
    expect(status.name).toBe('heartbeat');
    expect(status.outcomes).toEqual(['done']);
    expect(status.scheduleEvents).toEqual([
      {
        routine: 'heartbeat',
        slotMs: 1760000060000,
        disposition: 'dispatched',
        count: 1,
        reason: 'timer',
        recordedAtMs: 1760000060005,
      },
    ]);
  });

  it('workflow.list projects run rows and the optional current node', async () => {
    const { port, calls } = portWithWrites({
      'workflow.list': () => ({
        ok: true,
        runs: [
          {
            runId: 'wf-0001',
            graphId: 'wf-0001',
            name: 'brief',
            state: 'running',
            currentNode: { nodeId: 'work', state: 'requested' },
            createdAtMs: 1760000000000,
            closedAtMs: null,
            onFailure: 'terminate',
            sender: 'user:acme',
          },
          {
            runId: 'wf-0002',
            graphId: null,
            name: null,
            state: 'completed',
            currentNode: null,
            createdAtMs: 1760000000000,
            closedAtMs: 1760000100000,
            onFailure: null,
            sender: null,
          },
        ],
      }),
    });
    const api = createAgentsApi(port);

    const runs = await api.listWorkflows('worker');
    expect(runs).toHaveLength(2);
    expect(runs[0]).toMatchObject({
      runId: 'wf-0001',
      currentNode: { nodeId: 'work', state: 'requested' },
    });
    expect(runs[1]).toMatchObject({ currentNode: null, closedAtMs: 1760000100000 });
    expect(calls).toEqual([
      { method: 'workflow.list', params: { agent: 'worker' } },
    ]);
  });

  it('home-census projects the selected row and honest empty-result fallback', async () => {
    const row = {
      actor: 'worker',
      uri: 'agent:acme:node-1:worker',
      homeProvisioned: true,
      explicitConfig: true,
      configInsideHome: true,
      cwdInsideHome: false,
      residentOrDsh: false,
      transferReceived: false,
      mode: 'p2',
      legacyReason: null,
      harness: 'claude',
      lastHarness: 'claude',
    };
    const { port, calls } = portWithWrites({
      'agent.home-census': () => ({
        ok: true,
        readAtMs: 1760000000000,
        registry: { path: '/tmp/x', version: 1 },
        agents: [row],
        totals: { agents: 1, byModeHarness: [], homeUnprovisioned: 0 },
      }),
    });
    const api = createAgentsApi(port);

    const census = await api.homeCensus('worker');
    expect(census.row).toMatchObject({ actor: 'worker', mode: 'p2', cwdInsideHome: false });
    expect(census.totals).toEqual({ agents: 1, homeUnprovisioned: 0 });
    expect(calls).toEqual([
      { method: 'agent.home-census', params: { actor: 'worker' } },
    ]);

    const empty = portWithWrites({
      'agent.home-census': () => ({
        ok: true,
        readAtMs: 1760000000000,
        registry: { path: '/tmp/x', version: 1 },
        agents: [],
        totals: { agents: 0, byModeHarness: [], homeUnprovisioned: 0 },
      }),
    });
    const emptyCensus = await createAgentsApi(empty.port).homeCensus('worker');
    expect(emptyCensus.row).toBeNull();
    expect(emptyCensus.totals).toEqual({ agents: 0, homeUnprovisioned: 0 });
  });

  it('stop maps the removed list and keeps the empty removal legal', async () => {
    const removed = portWithWrites({
      'agent.stop': () => ({ ok: true, removed: ['claude:worker'] }),
    });
    const idle = portWithWrites({
      'agent.stop': () => ({ ok: true, removed: [] }),
    });

    const outcome = await createAgentsApi(removed.port).stop('worker');
    expect(outcome.removed).toEqual(['claude:worker']);
    expect(removed.calls[0]).toEqual({ method: 'agent.stop', params: { name: 'worker' } });

    await expect(createAgentsApi(idle.port).stop('worker')).resolves.toEqual({ removed: [] });
  });

  it('start forwards harness/provider/model to the daemon authority', async () => {
    const { port, calls } = portWithWrites({
      'agent.start': () => ({ ok: true, accepted: true, running: false }),
    });
    await createAgentsApi(port).start('worker', {
      harness: 'codex',
      provider: 'deepseek',
      model: 'deepseek-chat',
    });
    expect(calls[0]).toEqual({
      method: 'agent.start',
      params: {
        name: 'worker',
        harness: 'codex',
        provider: 'deepseek',
        model: 'deepseek-chat',
      },
    });
  });

  it.each([true, false])('preserves explicit Pi approval %s alongside the directory', async (approve) => {
    const { port, calls } = portWithWrites({
      'agent.start': () => ({ ok: true, accepted: true, running: false }),
    });
    await createAgentsApi(port).start('worker', { harness: 'pi', cwd: '/tmp/project', approve });
    expect(calls).toEqual([{ method: 'agent.start',
      params: { name: 'worker', harness: 'pi', cwd: '/tmp/project', approve } }]);
  });

  it('rename only sends name and displayName and returns the renamed row', async () => {
    const row = { ...createRowFixture(), displayName: '新名字' };
    const { port, calls } = portWithWrites({
      'agent.rename': () => ({ ok: true, agent: row }),
    });
    const renamed = await createAgentsApi(port).rename('worker', '新名字');
    expect(calls[0]).toEqual({
      method: 'agent.rename',
      params: { name: 'worker', displayName: '新名字' },
    });
    expect(renamed).toMatchObject({ actor: 'worker', displayName: '新名字' });
  });

  it('reads exact Secret grants with the available read fence without claiming authentication', async () => {
    const expectedAgent = { uri: 'agent:owner:node:worker', createdAtMs: 10 };
    const { port, calls } = portWithWrites({
      'agent.secret.list': () => ({ ok: true, grants: [{
        actor: 'agent:owner:node:worker', grantId: 'model-anthropic', source: 'user-provider',
        entryId: 'work', fieldName: 'apiKey', environmentNames: ['ANTHROPIC_API_KEY'], revision: 3,
        ownerActor: null, ownerEntityToken: null,
      }] }),
    });
    const grants = await createAgentsApi(port).providerStatus('agent:owner:node:worker', expectedAgent);
    expect(calls[0]).toEqual({ method: 'agent.secret.list', params: { actor: 'agent:owner:node:worker', expectedAgent } });
    expect(grants[0]).toEqual({ grantId: 'model-anthropic', provider: 'anthropic', state: 'granted',
      code: null, owner: null, expiresAtMs: null, keyId: 'work:apiKey', revision: 3, unsealable: false });
  });

  it('grants and revokes exact Secret IDs, leaving the running process unchanged', async () => {
    const { port, calls } = portWithWrites({
      'agent.secret.grant': (params) => ({ ok: true, grant: { ...params, ownerActor: null, ownerEntityToken: null } }),
      'agent.secret.revoke': () => ({ ok: true, revoked: true }),
    });
    const api = createAgentsApi(port);
    const allowed = await api.allowProvider('worker', 'anthropic', {
      keyId: 'work:apiKey', grantId: 'model-anthropic', revision: 4,
    });
    const revoked = await api.revokeProvider('worker', 'model-anthropic');
    expect(calls).toEqual([
      { method: 'agent.secret.grant', params: { actor: 'worker', source: 'user-provider', entryId: 'work',
        fieldName: 'apiKey', grantId: 'model-anthropic', revision: 4, environmentNames: ['ANTHROPIC_API_KEY'] } },
      { method: 'agent.secret.revoke', params: { actor: 'worker', grantId: 'model-anthropic' } },
    ]);
    expect(allowed.restartApplied).toBe(false);
    expect(revoked.restartApplied).toBe(false);
  });

  it('native login/logout send only filenames and base64 documents', async () => {
    const { port, calls } = portWithWrites({
      'agent.provider.login': () => ({
        ok: true,
        actor: 'worker',
        provider: 'claude',
        files: [{ path: '/agent/.credentials.json', bytes: 3, replaced: false, unchanged: false }],
      }),
      'agent.provider.logout': () => ({
        ok: true,
        actor: 'worker',
        provider: 'claude',
        removed: ['.credentials.json'],
        absent: [],
      }),
    });
    const api = createAgentsApi(port);
    const outcome = await api.loginNative('worker', 'claude', [
      { filename: '.credentials.json', contentBase64: 'YWJj' },
    ]);
    await api.logoutNative('worker', 'claude');
    expect(outcome.files).toHaveLength(1);
    expect(calls[0]).toEqual({
      method: 'agent.provider.login',
      params: {
        actor: 'worker',
        provider: 'claude',
        files: [{ filename: '.credentials.json', contentBase64: 'YWJj' }],
      },
    });
    expect(calls[1]).toEqual({
      method: 'agent.provider.logout',
      params: { actor: 'worker', provider: 'claude' },
    });
  });
});

describe('write failure messages stay honest (implG2; implG3 declared-set wording)', () => {
  it('undeclared codes keep the generic honest fallback (never success)', () => {
    const message = agentFailureMessage(
      new AgentRequestError('DAEMON_NOT_READY', 'not declared anymore', 'dispatched'),
    );
    expect(message.text).toContain('DAEMON_NOT_READY');
    expect(message.text).not.toContain('成功');
  });

  it('unknown dispatch never reads as success', () => {
    const error = transportError('NETWORK_ERROR', 'network failure', 'unknown');
    const message = agentFailureMessage(error);
    expect(message.dispatch).toBe('unknown');
    expect(message.text).toContain('结果未知');
    expect(message.text).not.toContain('成功');
  });

  it('unsettled stop reads as 结果未知', () => {
    const stop = agentFailureMessage(
      new AgentRequestError('LIFECYCLE_OPERATION_UNSETTLED', 'wait elapsed', 'dispatched'),
    );
    expect(stop.text).toContain('结果未知');
  });

  it('known create codes surface their own wording', () => {
    const exists = agentFailureMessage(
      new AgentRequestError('AGENT_EXISTS', 'taken', 'dispatched', { agent: 'worker' }),
    );
    expect(exists.text).toContain('已被占用');

    const invalid = agentFailureMessage(
      new AgentRequestError('INVALID_ARGUMENT', 'name must be a non-empty string', 'not-dispatched'),
    );
    expect(invalid.dispatch).toBe('not-dispatched');
    expect(invalid.text).toContain('未发送');
  });
});

it('uses the explicit local creation name when the registry has no display override', async () => {
  const { port } = portWith([{ ...AGENTS_LIST_ROWS_DATA[1], actor: 'pi-test', displayName: null }]);
  const rows = await createAgentsApi(port).list();
  expect(rows[0].displayName).toBe('pi-test');
  expect(rows[0].uri).toBe(AGENTS_LIST_ROWS_DATA[1].uri);
});


it('projects a current startup refusal without inventing a stopped process', async () => {
  const { port } = portWith([{ ...AGENTS_LIST_ROW_NO_RUNSTATE[0],
    startupFailure: { operationId: 'failed-start', code: 'HARNESS_START_FAILED',
      message: 'configured Pi model is unavailable', failedAtMs: 1 },
  }]);
  const [row] = await createAgentsApi(port).list();
  expect(row.runState.runtime).toBe('unknown');
  expect(row.startupFailure).toEqual({ operationId: 'failed-start',
    message: '启动失败：未找到所选模型，请修改模型配置后重新启动。' });
});
