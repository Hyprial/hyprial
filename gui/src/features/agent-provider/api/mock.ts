/** Development-only Contract Mock adapter. No local business fixtures or live fallback. */
import { createHttpAgentProviderApi, type AgentProviderApi } from './index';
import examples from './examples.generated.json';
export type AgentProviderScenario = 'success' | 'empty' | 'error' | 'unconfigured';
export function syntheticProviderLoginFiles() {
  const params = examples['agent.provider.login'].find(item => item.kind === 'success')?.params;
  if (!params || Array.isArray(params) || !('files' in params)) throw new Error('Synthetic login example is unavailable');
  return params.files;
}
export function createAgentProviderMock(scenario: AgentProviderScenario): AgentProviderApi {
  return { ...createHttpAgentProviderApi(`/api/reference/mock-${scenario}`), mockBoundary: {} };
}
