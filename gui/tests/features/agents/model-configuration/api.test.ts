import { createAgentsApi, createContractAgentGateway, type AgentsApi } from '../../../../src/features/agents/impl/api';
import { AgentRequestError } from '../../../../src/features/agents/impl/api/agent-writes';
import { AGENTS_LIST_ROWS_DATA } from '../../../fixtures/agents/agent-list';
import { createStartupMockAgents } from '../../../../src/dev/mock/startup-agents';

const configuration = { authKind: 'connection', harness: 'pi', mode: 'headless', model: 'hy4-flash', connectionRef: '0123456789abcdef0123456789abcdef', configRevision: 3 } as const;
const request = { configuration, operationId: 'gui-configure-1' };
const state = { ok: true, agentId: 'id', member: 'actor', binding: null, cell: { cellId: 'cell', revision: 9, intentMatches: null }, cleanupPending: false, operation: null, runtime: 'not-verified' };
function setup() {
  const handlers = { create: vi.fn(async () => ({ ok: true, created: true, agent: AGENTS_LIST_ROWS_DATA[0], providerDefault: null, modelConfiguration: { ...state, cell: { ...state.cell, intentMatches: true }, binding: { configuration }, operation: { phase: 'finalized', operationId: request.operationId } } })), model: { status: vi.fn(async () => state), configure: vi.fn(async () => state), cancel: vi.fn(async () => state) }, provider: { auth: { list: vi.fn(async () => ({ ok: true, records: [] })) } } };
  return { handlers, api: createAgentsApi({ agent: handlers } as never) };
}
it('sends explicit create without legacy overrides and without start', async () => {
  const { api, handlers } = setup();
  await api.create!({ name: 'new', preferredHarness: 'pi', provider: null, model: null, modelConfiguration: request });
  expect(handlers.create).toHaveBeenCalledExactlyOnceWith({ name: 'new', preferredHarness: 'pi', modelConfiguration: request });
});
it('creates native identity with durable no-default marker', async () => {
  const { api, handlers } = setup();
  await api.create!({ name: 'new', preferredHarness: 'pi', provider: null, model: null, requireModelConfiguration: true });
  expect(handlers.create).toHaveBeenCalledExactlyOnceWith({ name: 'new', preferredHarness: 'pi', capabilities: { modelConfigurationRequired: true } });
});
it('retains partial create recovery identity rather than treating it as a retryable failure', async () => {
  const { api, handlers } = setup();
  handlers.create.mockRejectedValueOnce(new AgentRequestError('MODEL_CONFIGURATION_INCOMPLETE', 'partial', 'dispatched', { actor: 'agent:test:node:a.id', agentId: 'id', operationId: request.operationId, phase: 'bind', reason: 'configuration-required', causeCode: 'MODEL_AUTH_UNAVAILABLE' }));
  const result = await createContractAgentGateway({ api })!.create({ displayName: 'new', role: '', modelServiceId: '', organizationId: null, modelConfiguration: request });
  expect(result).toMatchObject({ kind: 'incomplete', actorUri: 'agent:test:node:a.id', agentId: 'id', operationId: request.operationId });
  expect(handlers.create).toHaveBeenCalledTimes(1);
});
it('uses the three existing model methods with exact revisions and operation ID', async () => {
  const { api, handlers } = setup();
  expect(await api.modelStatus!('actor')).toEqual(state);
  const input = { ...request, name: 'actor', expectedCellRevision: 9, expectedBindingRevision: 0 };
  await api.configureModel!(input);
  await api.cancelModel!({ name: 'actor', operationId: request.operationId, expectedBindingRevision: 0 });
  expect(handlers.model.configure).toHaveBeenCalledExactlyOnceWith(input);
  expect(handlers.model.cancel).toHaveBeenCalledTimes(1);
});
it('reads native targets only for the current agent, with explicit version', async () => {
  const { api, handlers } = setup();
  await api.nativeTargets!('actor');
  expect(handlers.provider.auth.list).toHaveBeenCalledExactlyOnceWith({ actor: 'actor', targetsVersion: 1 });
});
it('does not offer model ports when injected handlers are absent', () => {
  const api = createAgentsApi({ agent: {} } as never);
  expect(api.modelStatus).toBeUndefined(); expect(api.configureModel).toBeUndefined(); expect(api.cancelModel).toBeUndefined();
});
it('keeps startup preview legacy capabilities usable without advertising model bindings', async () => {
  const { api } = createStartupMockAgents();
  const capabilities: AgentsApi = api;
  expect(capabilities.modelStatus).toBeUndefined();
  expect(capabilities.configureModel).toBeUndefined();
  expect(capabilities.cancelModel).toBeUndefined();
  expect(capabilities.nativeTargets).toBeUndefined();
  const created = await api.create({ name: 'preview-worker', preferredHarness: 'pi', provider: 'deepseek', model: 'deepseek-flash' });
  expect(created.created).toBe(true);
  expect(await api.list()).toMatchObject([{ actor: created.actor, preferredHarness: 'pi', model: 'deepseek-flash' }]);
  await api.allowProvider(created.entity.uri, 'deepseek', { keyId: 'deepseek-official:DEEPSEEK_API_KEY', grantId: 'preview-grant', revision: 1 });
  expect(await api.providerStatus(created.entity.uri)).toHaveLength(1);
  expect(await api.start(created.actor, { harness: 'pi', provider: 'deepseek', model: 'deepseek-flash', cwd: '/preview/work', approve: true })).toMatchObject({ accepted: true, running: true });
  expect((await api.list())[0].runState.runtime).toBe('online');
  expect((await api.stop(created.actor)).removed).not.toHaveLength(0);
  expect((await api.list())[0].runState.runtime).toBe('offline');
});
