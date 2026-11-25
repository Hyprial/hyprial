/** Existing Secret grants are metadata, not authentication or process credential evidence.
 * The daemon resolves canonical URI requests and reports its local actor name.
 * Receipt fields identify the stored grant; actor is not a URI echo.
 */
import { SECRET_PROVIDER_ENVIRONMENTS, parseSecretEntryReference, secretEntryReference, secretProviderForEnvironments } from '../../../../connections';
import type { MethodAccess } from '../../../../../client';
import { callAgentMethod, type AgentsClient } from '../client';
import { AgentRequestError, toAgentRequestError } from '../agent-writes';
import type { AgentsApi, AgentProviderGrant } from '..';

function invalid(message: string): never {
  throw new AgentRequestError('INVALID_ARGUMENT', message, 'not-dispatched');
}

export function secretProviderActions(client: AgentsClient | undefined, access?: MethodAccess): Required<Pick<AgentsApi, 'providerStatus' | 'allowProvider' | 'revokeProvider'>> {
  return {
    async providerStatus(actor, expectedAgent) {
      try {
        const result = await callAgentMethod('agent.secret.list', client?.agent?.secret?.list,
          { actor, ...(expectedAgent ? { expectedAgent } : {}) }, access);
        return result.grants.filter(grant => grant.source === 'user-provider').map((grant): AgentProviderGrant => ({
          grantId: grant.grantId, provider: secretProviderForEnvironments(grant.environmentNames),
          state: 'granted', code: null, owner: null, expiresAtMs: null, unsealable: false,
          revision: grant.revision,
          keyId: grant.fieldName ? secretEntryReference(grant.entryId, grant.fieldName) : null,
        }));
      } catch (error) { throw toAgentRequestError(error); }
    },
    async allowProvider(actor, provider, options) {
      try {
        if (options?.expectedAgent) invalid('请刷新 Agent 后重新授权。');
        const target = options?.keyId ? parseSecretEntryReference(options.keyId) : null;
        const environment = SECRET_PROVIDER_ENVIRONMENTS[provider];
        const revision = options?.revision;
        if (!target || !environment || !options?.grantId || !Number.isSafeInteger(revision) || revision! < 1) {
          invalid('请刷新授权记录并重新选择 Key。');
        }
        const params = { actor, ...target, source: 'user-provider' as const,
          grantId: options.grantId, revision: revision!, environmentNames: [environment] };
        const result = await callAgentMethod('agent.secret.grant', client?.agent?.secret?.grant, params, access);
        const grant = result.grant;
        if (!grant.actor || (!actor.startsWith('agent:') && grant.actor !== actor) || grant.grantId !== params.grantId || grant.entryId !== target.entryId
          || grant.fieldName !== target.fieldName || grant.revision !== revision || grant.source !== 'user-provider'
          || grant.environmentNames.length !== 1 || grant.environmentNames[0] !== environment) {
          throw new AgentRequestError('AGENT_RESPONSE_INVALID', '授权回执与目标不一致，请刷新核对。', 'dispatched');
        }
        return { changed: true, provider, restartApplied: false };
      } catch (error) { throw toAgentRequestError(error); }
    },
    async revokeProvider(actor, grantId, expectedAgent) {
      try {
        if (expectedAgent) invalid('请刷新 Agent 后重新撤销授权。');
        const result = await callAgentMethod('agent.secret.revoke', client?.agent?.secret?.revoke, { actor, grantId }, access);
        return { changed: result.revoked, provider: '', restartApplied: false };
      } catch (error) { throw toAgentRequestError(error); }
    },
  };
}
