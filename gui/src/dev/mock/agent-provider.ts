/** Development-only synthetic provider login input. No local business fixtures or live fallback. */
import type { ProviderLoginFile } from '../../features/agent-provider';
import { contractExamples } from '../contract-mock';
export type AgentProviderScenario = 'success' | 'empty' | 'error' | 'unconfigured';
export function syntheticProviderLoginFiles(): readonly ProviderLoginFile[] {
  const params = contractExamples('agent.provider.login').find(item => item.kind === 'success')?.params;
  if (!params || Array.isArray(params) || typeof params !== 'object' || !('files' in params)) throw new Error('Synthetic login example is unavailable');
  return params.files as readonly ProviderLoginFile[];
}
