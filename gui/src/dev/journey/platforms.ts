/** Platform lifecycle, bindings and routes share ephemeral journey state. */
import type { AgentAdapterBindingsApi, AgentsApi } from '../../features/agents';
import type { PlatformIntegrationsApi } from '../../features/connections';
import { createMockPlatformIntegrationsApi } from '../mock/platform-integrations';
import { createJourneyRoutes } from './routes';

export function createJourneyPlatforms(agents: Pick<Required<AgentsApi>, 'list'>) {
  const lifecycle = createMockPlatformIntegrationsApi({ integrations: [] });
  const bindings = new Map<string, string>();
  const platformIntegrations: PlatformIntegrationsApi = {
    ...lifecycle,
    async list() {
      return (await lifecycle.list()).map(row => ({ ...row, boundAgent: bindings.get(row.name) ?? null }));
    },
    async start(name) {
      const row = (await lifecycle.list()).find(row => row.name === name);
      if (row?.authorization.state !== 'authorized') throw new Error('请先完成平台授权。');
      await lifecycle.start(name);
    },
    async removePreview(name) {
      return { ...await lifecycle.removePreview!(name), boundAgent: bindings.get(name) ?? null };
    },
    async remove(name, expected) {
      const current = await platformIntegrations.removePreview!(name);
      if (current.boundAgent !== expected.boundAgent || current.processRunning !== expected.processRunning) {
        throw new Error('平台接入已变化，请刷新后重新确认。');
      }
      await lifecycle.remove!(name, current);
      bindings.delete(name);
      routes.forget(name);
    },
  };
  const routes = createJourneyRoutes(platformIntegrations);
  const agentAdapterBindings: AgentAdapterBindingsApi = {
    routes: { available: true, api: routes.api },
    list: () => platformIntegrations.list(),
    async bind(name, actor) {
      if (!(await lifecycle.list()).some(row => row.name === name)) throw new Error('平台接入不存在。');
      if (!(await agents.list()).some(agent => agent.uri === actor)) throw new Error('Agent 不存在。');
      if ((bindings.get(name) ?? null) === actor) return;
      bindings.set(name, actor);
    },
    async unbind(name, actor) {
      if (!(await lifecycle.list()).some(row => row.name === name)) throw new Error('平台接入不存在。');
      if ((bindings.get(name) ?? null) !== actor) throw new Error('绑定已变化，请刷新后重新确认。');
      bindings.delete(name);
    },
  };
  return {
    platformIntegrations, agentAdapterBindings,
    removeAgent(actor: string) {
      for (const [name, bound] of bindings) if (bound === actor) bindings.delete(name);
    },
  };
}
