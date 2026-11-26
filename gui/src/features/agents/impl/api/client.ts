/**
 * GUI live A (2026-11-04): composition owns the generated client and transport.
 * Missing injected methods fail before dispatch. This adapter only projects results.
 * 2026-11-14: a refusal before dispatch (catalog) is thrown as MethodRefusedError
 * and carries no sentence; callers classify it with methodRefusal.
 * 2026-11-14 (verification): a missing handler is an absent port, not a refusal.
 * withPresentHandlers drops every AgentsApi member whose handler the injected
 * client lacks, so a consumer renders the control only when the member exists;
 * reaching callAgentMethod without a handler is a programming error.
 */
import { assertMethodAvailable, type MethodAccess } from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';
import { AgentRequestError, toAgentRequestError } from './agent-writes';
import type { AgentsApi } from './index';

export type AgentsClient = Pick<GeneratedClient, 'agent' | 'org' | 'routine' | 'workflow'>;

export const AGENTS_CLIENT_METHODS = [
  'agent.model.status', 'agent.model.configure', 'agent.model.cancel', 'agent.provider.auth.list',
  'agent.destroy', 'agent.destroy.preview',
  'agent.list', 'agent.create', 'agent.start', 'agent.stop', 'agent.rename',
  'agent.secret.list', 'agent.provider.default',
  'agent.secret.grant', 'agent.secret.revoke',
  'agent.provider.login', 'agent.provider.logout', 'agent.home-census',
  'routine.list', 'routine.status', 'workflow.list',
] as const;

export async function callAgentMethod<P, R>(
  method: typeof AGENTS_CLIENT_METHODS[number],
  handler: ((params: P) => Promise<R>) | undefined,
  params: P,
  access?: MethodAccess,
): Promise<R> {
  assertMethodAvailable(access, method, params);
  if (typeof handler !== 'function') {
    throw new TypeError(`${method} has no handler; the member must not exist`);
  }
  try {
    const result = await handler(params);
    if (!result || typeof result !== 'object' || ('ok' in result && result.ok !== true)) {
      throw new AgentRequestError('AGENT_RESPONSE_INVALID', '请求未返回契约成功结果。', 'dispatched', null, false);
    }
    return result;
  } catch (error) {
    throw toAgentRequestError(error);
  }
}

const HANDLER_OF: { readonly [K in keyof AgentsApi]-?: (client?: AgentsClient) => unknown } = {
  modelStatus: (client) => client?.agent?.model?.status,
  configureModel: (client) => client?.agent?.model?.configure,
  cancelModel: (client) => client?.agent?.model?.cancel,
  nativeTargets: (client) => client?.agent?.provider?.auth?.list,
  previewDestroy: (client) => client?.agent?.destroy?.preview,
  destroy: (client) => client?.agent?.destroy,
  list: (client) => client?.agent?.list,
  listGrants: (client) => client?.agent?.secret?.list,
  defaultProvider: (client) => client?.agent?.provider?.default,
  create: (client) => client?.agent?.create,
  stop: (client) => client?.agent?.stop,
  start: (client) => client?.agent?.start,
  rename: (client) => client?.agent?.rename,
  providerStatus: (client) => client?.agent?.secret?.list,
  allowProvider: (client) => client?.agent?.secret?.grant,
  revokeProvider: (client) => client?.agent?.secret?.revoke,
  loginNative: (client) => client?.agent?.provider?.login,
  logoutNative: (client) => client?.agent?.provider?.logout,
  listRoutines: (client) => client?.routine?.list,
  routineStatus: (client) => client?.routine?.status,
  listWorkflows: (client) => client?.workflow?.list,
  homeCensus: (client) => client?.agent?.homeCensus,
};

export function withPresentHandlers(
  client: AgentsClient | undefined,
  api: Required<AgentsApi>,
): AgentsApi {
  const present = (Object.keys(api) as (keyof AgentsApi)[])
    .filter((name) => typeof HANDLER_OF[name](client) === 'function')
    .map((name) => [name, api[name]]);
  return Object.fromEntries(present) as AgentsApi;
}
