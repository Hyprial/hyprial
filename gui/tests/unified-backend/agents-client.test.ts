/** Generated-client test doubles only; no live daemon, credential or browser acceptance is claimed. */
import { describe, expect, it, vi } from 'vitest';
import { DomainError, TransportError, type RequestDiagnostics } from '../../src/client';
import type {
  AgentCreateResult, AgentListResult, AgentProviderDefaultResult,
  AgentStartResult, AgentStopResult,
} from '../../src/client/generated/types';
import {
  createAgentsApi, createContractAgentGateway,
  createRuntimeActionHandler, directoryRowToView,
} from '../../src/features/agents/impl/api';
import { agentsTestClient } from './agents-test-client';

const agentEntity = {
  actor: 'worker', owner: 'owner@example.test', machine: 'test-node',
  uri: 'agent:owner@example.test:test-node:worker', capabilities: {},
  config: null, createdAtMs: 10, cwd: null, harness: null, harnessArgs: {},
  heartbeatAgeSeconds: null, hosted: false, hostedBy: null, lastActiveAtMs: null,
  lastHarness: null, lastSessionId: null, model: null,
  preferredHarness: null, provider: null, runtime: null, sessionId: null, status: 'created',
  activityHints: { agentHomeMtimeMs: null, createdAtMs: 10, hasLastSessionId: false },
} satisfies AgentListResult['agents'][number] & AgentCreateResult['agent'];
const agentList = { ok: true, agents: [agentEntity], agentsWithoutRoutine: [] } satisfies AgentListResult;
const draft = { displayName: 'worker', role: '', modelServiceId: '', organizationId: null };
const createInput = { name: 'worker', provider: null, model: null, preferredHarness: null };

function diagnostics(method: string, dispatch: RequestDiagnostics['dispatch'] = 'dispatched'): RequestDiagnostics {
  return { method, dispatch, requestId: 'request-original', status: 403,
    catalogDigest: 'sha256:test', attempts: 1, startedAt: 1, durationMs: 2 };
}

describe('agent adapters consume an injected generated client', () => {
  it('reads list/detail facts without inventing credential or running readiness', async () => {
    const { client, calls } = agentsTestClient(async () => agentList);
    const rows = await createAgentsApi(client).list();
    expect(calls.map(call => call.method)).toEqual(['agent.list']);
    expect(rows[0]).toMatchObject({ actor: 'worker', owner: 'owner@example.test', runState: { runtime: 'unknown' } });
    expect(directoryRowToView(rows[0])).toMatchObject({ availability: 'unknown', model: { kind: 'unknown' } });
  });

  it('shares the supplied generated create/start/stop methods and never retries writes', async () => {
    const results = {
      'agent.create': { ok: true, created: true, agent: agentEntity, providerDefault: null } satisfies AgentCreateResult,
      'agent.start': { ok: true, accepted: true, running: false, warnings: ['provider pending'] } satisfies AgentStartResult,
      'agent.stop': { ok: true, removed: [] } satisfies AgentStopResult,
    };
    const { client, calls } = agentsTestClient(async method => results[method as keyof typeof results]);
    const api = createAgentsApi(client);
    expect(await api.create(createInput)).toMatchObject({ actor: 'worker', created: true, providerDefault: { captured: false } });
    expect(await api.start('worker')).toEqual({ accepted: true, running: false, warnings: ['provider pending'] });
    expect(await api.stop('worker')).toEqual({ removed: [] });
    expect(calls.map(call => [call.method, call.params, call.options?.retry])).toEqual([
      ['agent.create', createInput, 'never'], ['agent.start', { name: 'worker' }, 'never'],
      ['agent.stop', { name: 'worker' }, 'never'],
    ]);
  });

  it.each(['create', 'start', 'stop'] as const)('preserves owner mismatch for %s without retry or success', async operation => {
    const original = new DomainError('PRINCIPAL_MISMATCH', 'owner mismatch', { owner: 'other' }, diagnostics(`agent.${operation}`, 'not-dispatched'));
    const { client, calls } = agentsTestClient(async () => { throw original; });
    const api = createAgentsApi(client);
    const attempt = operation === 'create' ? api.create(createInput) : api[operation]('worker');
    await expect(attempt).rejects.toMatchObject({ code: original.code, dispatch: 'not-dispatched', data: original.data, diagnostics: original.diagnostics });
    expect(calls).toHaveLength(1);
  });

  it.each(['AGENT_NOT_FOUND', 'PROVIDER_NOT_READY', 'CALLER_NOT_AUTHORIZED'])('shows a start refusal for %s', async code => {
    const { client, calls } = agentsTestClient(async () => { throw new DomainError(code, 'refused', null, diagnostics('agent.start')); });
    const outcome = await createRuntimeActionHandler(createAgentsApi(client))('start', 'missing');
    expect(outcome.tone).toBe('error');
    expect(outcome.code).toBe(code);
    expect(calls).toHaveLength(1);
  });

  it('does not render accepted=false as a successful start', async () => {
    const { client } = agentsTestClient(async () => ({ ok: true, accepted: false, running: false } satisfies AgentStartResult));
    const outcome = await createRuntimeActionHandler(createAgentsApi(client))('start', 'worker');
    expect(outcome).toMatchObject({ tone: 'error', dispatched: true });
    expect(outcome.text).toContain('未受理');
  });

  it.each(['create', 'start', 'stop'] as const)('retains original diagnostics after unknown %s', async operation => {
    const original = new TransportError('timeout', 'TIMEOUT', 'response lost', diagnostics(`agent.${operation}`, 'unknown'));
    const { client, calls } = agentsTestClient(async () => { throw original; });
    const api = createAgentsApi(client);
    const after = vi.fn();
    const result = operation === 'create'
      ? await createContractAgentGateway({ api, afterCreate: after }).create(draft)
      : await createRuntimeActionHandler(api, after)(operation, 'worker');
    expect(result).toMatchObject(operation === 'create' ? { kind: 'unknown' } : { tone: 'error', dispatched: true });
    expect('message' in result ? result.message : 'text' in result ? result.text : '').toContain('结果未知');
    expect(result).toMatchObject({ code: 'TIMEOUT', diagnostics: original.diagnostics });
    expect(after).toHaveBeenCalledOnce();
    expect(calls).toHaveLength(1);
    const direct = operation === 'create' ? api.create(createInput) : api[operation]('worker');
    await expect(direct).rejects.toMatchObject({ diagnostics: original.diagnostics, dispatch: 'unknown' });
    expect(calls).toHaveLength(2);
  });

  it('distinguishes provider configuration, grant inventory and runtime readiness', async () => {
    const provider = { ok: true, provider: null, previous: null } satisfies AgentProviderDefaultResult;
    const { client } = agentsTestClient(async method => method === 'agent.list' ? agentList
      : method === 'agent.secret.list' ? { ok: true, grants: [] } : provider);
    const api = createAgentsApi(client);
    expect(await api.defaultProvider()).toEqual({ ready: true, provider: null });
    expect(await api.listGrants('worker')).toEqual([]);
    expect(directoryRowToView((await api.list())[0]).availability).toBe('unknown');
  });

  it('does not expose listLinks on the agent adapter', () => {
    const { client } = agentsTestClient(async () => ({ ok: true }));
    const api = createAgentsApi(client);
    expect('listLinks' in api).toBe(false);
  });

  it.each(['listRoutines', 'listWorkflows', 'homeCensus'] as const)('keeps %s empty and failure distinct', async operation => {
    const { client } = agentsTestClient(async () => ({ ok: true, routines: [], runs: [], agents: [], totals: {} }));
    const api = createAgentsApi(client);
    expect(await api[operation]('worker')).toEqual(operation === 'homeCensus'
      ? { readAtMs: null, row: null, totals: { agents: 0, homeUnprovisioned: 0 } } : []);
    const original = new DomainError('AGENT_NOT_FOUND', 'missing', null, diagnostics(operation));
    const failed = agentsTestClient(async () => { throw original; });
    await expect(createAgentsApi(failed.client)[operation]('worker')).rejects.toMatchObject({ code: original.code, diagnostics: original.diagnostics });
  });

  it('defines no update or delete on the contract gateway even with injected generated writes', async () => {
    const { client, calls } = agentsTestClient(async () => { throw new Error('must not call any method'); });
    const gateway = createContractAgentGateway({ api: createAgentsApi(client) });
    expect(gateway.update).toBeUndefined();
    expect(gateway.delete).toBeUndefined();
    expect(calls).toEqual([]);
  });

  it('an absent injection is an absent port: no member exists and nothing is refused', () => {
    expect(Object.keys(createAgentsApi())).toEqual([]);
    expect(createAgentsApi().list).toBeUndefined();
  });

  it('omits exactly the members whose handler the injected client lacks', () => {
    const { client } = agentsTestClient(async () => ({ ok: true }));
    const api = createAgentsApi({ ...client, agent: { ...client.agent, stop: undefined } } as never);
    expect(api.stop).toBeUndefined();
    expect(typeof api.start).toBe('function');
    expect(typeof api.list).toBe('function');
  });
});
