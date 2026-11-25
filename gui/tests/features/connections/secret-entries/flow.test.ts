import { describe, expect, it, vi } from 'vitest';
import type { GeneratedClient } from '../../../../src/client/generated';
import { createSharedCredentialsApiFromClient } from '../../../../src/features/connections';
import { createAgentsApi } from '../../../../src/features/agents/impl/api';

function setup() {
  const calls: { method: string; params: unknown }[] = [];
  const invoke = (method: string, result: (params: any) => unknown) => async (params: any) => {
    calls.push({ method, params });
    return result(params);
  };
  const client = {
    agent: { secret: {
      providerList: invoke('agent.secret.provider-list', () => ({ ok: true, entries: [
        { entryId: 'team-key', fieldNames: ['DEEPSEEK_API_KEY'] },
        { entryId: 'deepseek-name-only', fieldNames: ['apiKey'] },
      ] })),
      providerWrite: invoke('agent.secret.provider-write', p => ({ ok: true, entryId: p.entryId, fieldNames: [p.fieldName] })),
      list: invoke('agent.secret.list', () => ({ ok: true, grants: [] })),
      grant: invoke('agent.secret.grant', p => ({ ok: true, grant: { ...p, ownerActor: null, ownerEntityToken: null } })),
      revoke: invoke('agent.secret.revoke', () => ({ ok: true, revoked: true })),
    } },
  } as unknown as GeneratedClient;
  return { calls, keys: createSharedCredentialsApiFromClient(client), agents: createAgentsApi(client) };
}

describe('saved secret → exact Agent grant', () => {
  it('lists ungranted entries without guessing vendor from the entry name or inventing timestamps', async () => {
    const { keys, calls } = setup();
    const entries = await keys.listKeys();
    expect(entries).toMatchObject([
      { keyId: 'team-key:DEEPSEEK_API_KEY', provider: 'deepseek', createdAt: null, lastUsedAt: null },
      { keyId: 'deepseek-name-only:apiKey', provider: '', createdAt: null },
    ]);
    expect(calls.map(c => c.method)).toEqual(['agent.secret.provider-list']);
  });

  it('checks a new name before saving and replaces an existing field only on request', async () => {
    const { keys, calls } = setup();
    expect((await keys.addKey('deepseek', 'new-key', 'synthetic-secret')).status).toBe('saved');
    expect((await keys.replaceKey('team-key:DEEPSEEK_API_KEY', 'synthetic-replacement')).status).toBe('saved');
    expect(calls).toEqual([
      { method: 'agent.secret.provider-list', params: {} },
      { method: 'agent.secret.provider-write', params: { entryId: 'new-key', fieldName: 'DEEPSEEK_API_KEY', value: 'synthetic-secret' } },
      { method: 'agent.secret.provider-write', params: { entryId: 'team-key', fieldName: 'DEEPSEEK_API_KEY', value: 'synthetic-replacement' } },
    ]);
    expect(keys.deleteKey).toBeUndefined();
  });

  it('grants metadata, never the secret value, using the revision selected by the controller', async () => {
    const { agents, calls } = setup();
    await agents.allowProvider('actor-1', 'deepseek', { keyId: 'team-key:DEEPSEEK_API_KEY', grantId: 'held-grant', revision: 4 });
    expect(calls).toEqual([{ method: 'agent.secret.grant', params: {
      actor: 'actor-1', grantId: 'held-grant', source: 'user-provider', entryId: 'team-key',
      fieldName: 'DEEPSEEK_API_KEY', environmentNames: ['DEEPSEEK_API_KEY'], revision: 4,
    } }]);
  });

  it('does not silently drop an unsupported incarnation fence', async () => {
    const { agents, calls } = setup();
    await expect(agents.allowProvider('actor-1', 'deepseek', {
      keyId: 'team-key:DEEPSEEK_API_KEY', expectedAgent: { uri: 'actor-1', createdAtMs: 1 },
    })).rejects.toMatchObject({ code: 'INVALID_ARGUMENT', dispatch: 'not-dispatched' });
    expect(calls).toEqual([]);
  });

  it('revokes the exact grant and leaves its saved entry intact', async () => {
    const { keys, calls } = setup();
    expect((await keys.revokeGrant('actor-1', 'grant-exact')).status).toBe('saved');
    expect(calls).toEqual([{ method: 'agent.secret.revoke', params: { actor: 'actor-1', grantId: 'grant-exact' } }]);
  });
  it.each([undefined, 0, -1, 1.5, Number.NaN])('refuses invalid revision %s without writing', async revision => {
    const { agents, calls } = setup();
    await expect(agents.allowProvider('actor-1', 'deepseek', {
      keyId: 'team-key:DEEPSEEK_API_KEY', grantId: 'held-grant', revision,
    })).rejects.toMatchObject({ code: 'INVALID_ARGUMENT', dispatch: 'not-dispatched' });
    expect(calls).toEqual([]);
  });

  it('does not write reserved member credentials or unsafe entry names', async () => {
    const { keys, calls } = setup();
    expect((await keys.addKey('hyprial', 'member-key', 'synthetic')).status).toBe('failed');
    expect((await keys.addKey('deepseek', '../outside', 'synthetic')).status).toBe('failed');
    expect(calls).toEqual([]);
  });

  it('accepts the daemon local actor name in a receipt for a canonical URI request', async () => {
    const grant = vi.fn(async params => ({ ok: true, grant: { ...params, actor: 'ds-agent1',
      ownerActor: null, ownerEntityToken: null } }));
    const agents = createAgentsApi({ agent: { secret: { grant } } } as unknown as GeneratedClient);
    const actor = 'agent:owner:node:a.aaaaaaaaaaaaaaaaaaaaaaaaaa';
    await expect(agents.allowProvider(actor, 'deepseek', {
      keyId: 'team-key:DEEPSEEK_API_KEY', grantId: 'model-deepseek', revision: 1,
    })).resolves.toMatchObject({ changed: true, restartApplied: false });
    expect(grant.mock.calls[0][0].actor).toBe(actor);
  });

});
