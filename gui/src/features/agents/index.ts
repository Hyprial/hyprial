/**
 * Decision record (GUI deep modules B4, 2026-11-08):
 * - This entry is the only cross-module surface of the agents feature;
 *   implementations live under impl/ and tests may white-box them.
 * - agents.css loads with this entry so page compositions stop importing
 *   styles from inside the implementation tree.
 * - impl/api/mock-style fixtures stay outside this entry; development
 *   compositions import them directly until phase C moves mocks into dev/.
 */

import './impl/agents.css';

export { AgentDetail } from './impl/AgentDetail';
export { AgentForm, keyCompatibleWithHarness } from './impl/AgentForm';
export { AgentList } from './impl/AgentList';
export { CredentialsTab } from './impl/detail/CredentialsTab';
export { OverviewTab } from './impl/detail/OverviewTab';
export { PlatformBindingsTab } from './impl/detail/PlatformBindingsTab';
export { RelationsTab } from './impl/detail/RelationsTab';
export {
  createLocalAgentGateway,
  type AgentDraft,
  type AgentCredentialPlan,
  type AgentMutationGateway,
  type AgentMutationResult,
} from './impl/AgentMutationGateway';
export {
  createAgentsApi,
  createContractAgentGateway,
  createRuntimeActionHandler,
  directoryRowToExtension,
  directoryRowToView,
  grantRowsToProviderGrants,
  type AgentDirectoryRow,
  type AgentsApi,
} from './impl/api';
export { useAgentDirectory } from './impl/api/use-agent-directory';
export { mergeOrganizationAgents } from './impl/org-directory';
export {
  createAgentAdapterBindingsFromClient,
  type AgentAdapterBindingRow,
  type AgentAdapterBindingsApi,
} from './impl/api/agent-adapter-bindings';
export {
  agentFailureMessage,
  startFailureMessage,
  toAgentRequestError,
} from './impl/api/agent-writes';
export {
  getAgentExtension,
  type AgentExtensionSeed,
  type AgentExtensionView,
} from './impl/agent-extensions';
