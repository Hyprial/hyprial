/** One simulated Key/grant authority; no secret values or native Auth state are retained. */
import type { AgentsApi } from '../../features/agents';
import type { SharedCredentialsApi } from '../../features/connections';
import { createMockSharedCredentialsApi } from '../mock/agent-settings';

type Grant = Awaited<ReturnType<Required<AgentsApi>['providerStatus']>>[number];

export function createJourneyCredentials(directory: Pick<Required<AgentsApi>, 'list'>) {
  const stored = createMockSharedCredentialsApi({ entries: [], authGrants: [] });
  const grants = new Map<string, Map<string, Grant>>();
  async function actorOf(selector: string) {
    const row = (await directory.list()).find(agent => agent.uri === selector || agent.actor === selector);
    if (!row) throw new Error('Agent 不存在。');
    return row.uri;
  }
  async function revoke(agent: string, grantId: string) {
    const actor = await actorOf(agent);
    const previous = [...(grants.get(actor)?.values() ?? [])].find(grant => grant.grantId === grantId);
    if (!previous) throw new Error('授权不存在。');
    grants.get(actor)!.set(previous.provider, { ...previous, state: 'revoked', unsealable: false });
  }
  const sharedCredentials: SharedCredentialsApi = {
    ...stored,
    async listKeys() {
      const entries = await stored.listKeys();
      return entries.map(entry => ({
        ...entry,
        grants: [...grants].flatMap(([actor, slots]) => [...slots.values()]
          .filter(grant => grant.keyId === entry.keyId && grant.state === 'ok')
          .map(grant => ({ agent: actor, agentId: null, grantId: grant.grantId, provider: grant.provider, state: 'active' }))),
      }));
    },
    async revokeGrant(agent, grantId) {
      await revoke(agent, grantId);
      return { status: 'saved', message: '授权已撤销。' };
    },
  };
  const provider: Pick<Required<AgentsApi>, 'allowProvider' | 'revokeProvider' | 'providerStatus'> = {
    async allowProvider(agent, provider, options) {
      const actor = await actorOf(agent);
      const entries = await stored.listKeys();
      const entry = entries.find(key => key.provider === provider && (!options?.keyId || key.keyId === options.keyId));
      if (!entry) throw new Error('Key 不存在。');
      const slots = grants.get(actor) ?? new Map<string, Grant>();
      const previous = slots.get(provider);
      if (previous?.state === 'ok' && previous.keyId !== entry.keyId && !options?.replace) throw new Error('请确认更换授权。');
      slots.set(provider, { grantId: options?.grantId ?? provider, revision: options?.revision ?? (previous?.revision ?? 0) + 1, provider, state: 'ok', code: null,
        owner: 'preview@example.invalid', keyId: entry.keyId, expiresAtMs: null, unsealable: true });
      grants.set(actor, slots);
      return { changed: previous?.keyId !== entry.keyId || previous.state !== 'ok', provider, restartApplied: false };
    },
    async revokeProvider(agent, grantId) {
      await revoke(agent, grantId);
      return { changed: true, provider: '', restartApplied: false };
    },
    async providerStatus(agent) {
      return [...(grants.get(await actorOf(agent))?.values() ?? [])].map(row => ({ ...row }));
    },
  };
  return { sharedCredentials, provider, removeAgent: (actor: string) => { grants.delete(actor); } };
}
