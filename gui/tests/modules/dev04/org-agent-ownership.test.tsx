/**
 * Decisions under test (owner, 2026-11-21)
 * - "我的" Agent means the current user's Agent, including that user's Agents on other devices.
 * - When the current user is unknown (org.network failed, or self.owner is null/empty) no row gets a
 *   guessed `mine`.
 */
import type { OrgStore } from '../../../src/features/org/impl/domain/context';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';
import { createOrgLiveSourceForPort } from '../../support/org-backend';

const ORG = 'group-hyprial-dev';
const OTHER_ORG = 'group-acme-lab';
const HERE = 'a.' + 'a'.repeat(26);
const THERE = 'a.' + 'b'.repeat(26);
const GUEST = 'a.' + 'c'.repeat(26);
const NO_ORG = 'a.' + 'd'.repeat(26);

const LIST_RESULT = {
  orgs: [
    { org: ORG, spaceId: 'space-team', role: 'owner', devices: 2, state: 'active' },
    { org: OTHER_ORG, spaceId: null, role: null, devices: 0, state: 'legacy-active' },
  ],
  leftOrgs: [],
};

const TARGETS = {
  targets: [
    {
      targetKind: 'agent', targetUri: `agent:alice:node-a:${HERE}`, actor: `agent:alice:node-a:${HERE}`,
      status: 'online', deliverable: true, hosted: false, hostedBy: null, agentId: HERE,
      displayName: 'here', orgs: [ORG, OTHER_ORG],
    },
    {
      targetKind: 'agent', targetUri: `agent:alice:node-b:${THERE}`, actor: `agent:alice:node-b:${THERE}`,
      status: 'unknown', deliverable: true, agentId: THERE, displayName: 'there', orgs: [ORG],
    },
    {
      targetKind: 'agent', targetUri: `agent:bob:node-a:${GUEST}`, actor: `agent:bob:node-a:${GUEST}`,
      status: 'offline', deliverable: true, hosted: true, hostedBy: 'transfer-receive', agentId: GUEST,
      displayName: 'guest', orgs: [ORG],
    },
    {
      targetKind: 'agent', targetUri: `agent:alice:node-a:${NO_ORG}`, actor: `agent:alice:node-a:${NO_ORG}`,
      status: 'offline', deliverable: true, hosted: false, hostedBy: null, agentId: NO_ORG,
      displayName: 'alone',
    },
  ],
  orgDirectoryStatus: { [ORG]: 'ok', [OTHER_ORG]: 'partial' },
};

function networkWith(self: Record<string, unknown>) {
  return {
    orgs: [ORG], unavailableOrgs: [], dataProblems: [], directory: {}, self,
  };
}

function memoryStore(initial: OrgDomainSnapshot): OrgStore {
  let current = initial;
  return { get: () => current, update: (next) => { current = next(current); } };
}

function fakePort(self: Record<string, unknown>) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  return {
    calls,
    async request(method: string, params: Record<string, unknown>) {
      calls.push({ method, params });
      if (method === 'org.list') return LIST_RESULT;
      if (method === 'targets') return TARGETS;
      if (method === 'org.network') return networkWith(self);
      if (method === 'hosts') return { hosts: [] };
      return {};
    },
  };
}

async function refreshed(self: Record<string, unknown>) {
  const port = fakePort(self);
  const source = createOrgLiveSourceForPort(port);
  const store = memoryStore(source.initial);
  await source.createGateway(store).refresh(null);
  return { port, state: store.get() };
}

const SELF_ALICE_NODE_A = { deviceId: 'node-a', owner: 'user:alice' };

describe('org Agent ownership under the owner rule', () => {
  it('(i) my Agent on this device is mine', async () => {
    const { state } = await refreshed(SELF_ALICE_NODE_A);
    const agent = state.details[ORG]?.agents?.find((row) => row.id === HERE);
    expect(agent).toMatchObject({ mine: true, machineLabel: 'node-a', ownerLabel: 'user:alice' });
  });

  it('(ii) my Agent on another device is mine', async () => {
    const { state } = await refreshed(SELF_ALICE_NODE_A);
    const agent = state.details[ORG]?.agents?.find((row) => row.id === THERE);
    expect(agent).toMatchObject({ mine: true, machineLabel: 'node-b', ownerLabel: 'user:alice' });
  });

  it("(iii) another user's Agent hosted on this device is not mine", async () => {
    const { state } = await refreshed(SELF_ALICE_NODE_A);
    const agents = state.details[ORG]?.agents ?? [];
    expect(agents.find((row) => row.id === GUEST)).toMatchObject({
      mine: false, machineLabel: 'node-a', ownerLabel: 'user:bob',
    });
    expect(agents.filter((row) => row.mine).map((row) => row.id)).toEqual([HERE, THERE]);
  });

  it.each([
    ['self is empty', {}],
    ['owner is missing', { deviceId: 'node-a' }],
    ['owner is blank', { deviceId: 'node-a', owner: '' }],
    ['owner is null', { deviceId: 'node-a', owner: null }],
  ])('(iv) mine is undetermined when %s', async (_label, self) => {
    const { state } = await refreshed(self);
    const agents = state.details[ORG]?.agents ?? [];
    expect(agents.length).toBeGreaterThan(0);
    for (const row of agents) expect(row.mine).toBeUndefined();
  });

  it('(iv) mine is undetermined when org.network itself fails', async () => {
    const port = fakePort(SELF_ALICE_NODE_A);
    const failing = {
      ...port,
      async request(method: string, params: Record<string, unknown>) {
        if (method === 'org.network') throw new Error('network down');
        return port.request(method, params);
      },
    };
    const source = createOrgLiveSourceForPort(failing);
    const store = memoryStore(source.initial);
    await source.createGateway(store).refresh(null);
    const agents = store.get().details[ORG]?.agents ?? [];
    expect(agents.length).toBeGreaterThan(0);
    for (const row of agents) expect(row.mine).toBeUndefined();
  });

  it('(v) refresh issues exactly one agent-related request and never agent.list', async () => {
    const { port } = await refreshed(SELF_ALICE_NODE_A);
    expect(port.calls.filter((call) => call.method === 'agent.list')).toEqual([]);
    expect(port.calls.filter((call) => call.method === 'targets')).toEqual([
      { method: 'targets', params: { kind: 'agent' } },
    ]);
  });

  it('(vi) names, machines, memberships and directory status come from the targets rows', async () => {
    const { state } = await refreshed(SELF_ALICE_NODE_A);
    expect(state.details[ORG]?.agents?.map((row) => [row.id, row.name, row.machineLabel])).toEqual([
      [HERE, 'here', 'node-a'], [THERE, 'there', 'node-b'], [GUEST, 'guest', 'node-a'],
    ]);
    expect(state.agentOrgs).toEqual({
      [HERE]: [ORG, OTHER_ORG], [THERE]: [ORG], [GUEST]: [ORG], [NO_ORG]: [],
    });
    expect(state.orgAgentDirectoryStatus).toEqual({ [ORG]: 'ok', [OTHER_ORG]: 'partial' });
    expect(state.orgAgentDirectory?.[OTHER_ORG]?.map((row) => row.id)).toEqual([HERE]);
  });

  it('(vi) directory rows of orgs without a detail read carry no guessed mine', async () => {
    const { state } = await refreshed(SELF_ALICE_NODE_A);
    const row = state.orgAgentDirectory?.[OTHER_ORG]?.[0];
    expect(row).toBeDefined();
    expect(row).not.toHaveProperty('mine');
  });
});
