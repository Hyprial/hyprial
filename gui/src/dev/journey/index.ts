/** Shared in-memory journey authority; deletion is injected, never a pretend HTTP method. */
import type { AgentMutationGateway } from '../../features/agents';
import { createStartupMockAgents, type MockAgentRow } from '../mock/startup-agents';
import { createJourneyCredentials } from './credentials';
import { createJourneyPlatforms } from './platforms';
import { createProviderLoginFilePicker } from '../../features/agent-provider';

export function createAgentJourney(onChanged?: (agents: readonly MockAgentRow[]) => void) {
  const runtime = createStartupMockAgents(undefined, onChanged);
  const credentials = createJourneyCredentials(runtime.api);
  const agents = { ...runtime.api, ...credentials.provider };
  const pickLoginFiles = createProviderLoginFilePicker();
  const platforms = createJourneyPlatforms(agents);
  const agentDelete: AgentMutationGateway['delete'] = async (selector) => {
    const agent = (await agents.list()).find(row => row.actor === selector || row.uri === selector);
    if (!agent) return { kind: 'failed', message: 'Agent 不存在。' };
    platforms.removeAgent(agent.uri);
    credentials.removeAgent(agent.uri);
    runtime.remove(agent.uri);
    return { kind: 'deleted', agentId: agent.actor };
  };
  return {
    agents,
    agentDelete,
    agentProviderLoginFilePicker: pickLoginFiles,
    agentProviderLoginFiles: async (_actor: string, provider: string) => pickLoginFiles(provider),
    chatTargets: runtime.chatTargets,
    sharedCredentials: { ...credentials.sharedCredentials, nativeAuth: runtime.nativeAuth },
    platformIntegrations: platforms.platformIntegrations,
    agentAdapterBindings: platforms.agentAdapterBindings,
  };
}
