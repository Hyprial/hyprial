import { describe, expect, it } from 'vitest';
import type { OrgNetworkResult } from '../../../src/client/generated/types';
import { networkViewFor } from '../../../src/features/org/impl/domain/network/network-view';

const network: OrgNetworkResult = {
  orgs: ['group-alpha'],
  unavailableOrgs: [],
  dataProblems: [],
  directory: {
    'group-alpha': [{
      deviceId: 'device-a', owner: 'user:a', serverPublic: 'server-a', clientPublic: 'client-a',
      clientPublicByServer: {}, addressPresent: true, keyGeneration: 1,
      updatedAt: '2026-11-13T10:00:00Z', presence: 'online', machineFingerprint: 'laptop-a',
    }],
  },
  self: { deviceId: 'device-a', owner: 'user:a', serverPublic: 'server-a', addressPresent: true },
  mappedPeers: [{ peer: 'peer-b', endpoint: '10.0.0.2:1234', dialed: true }],
  sidecar: {
    policy: { mode: 'managed', source: 'local' },
    lastPoll: { atMs: 1794570000000, ok: true },
    slots: { poolSize: 8, free: 7, assigned: [] },
    restartRequired: false,
  },
  paneStatus: {
    directory: { available: true }, mappedPeers: { available: true }, sidecar: { available: true },
  },
};

describe('org network read model', () => {
  it('shows only the selected organization directory and preserves independent sidecar panes', () => {
    expect(networkViewFor('group-alpha', network)).toMatchObject({
      devices: [{ id: 'device-a', name: 'laptop-a', current: true, owner: 'user:a' }],
      mappedPeers: [{ peer: 'peer-b', endpoint: '10.0.0.2:1234', dialed: true }],
      sidecar: { policyMode: 'managed', slotsFree: 7, slotsTotal: 8, restartRequired: false },
      panes: { directory: true, mappedPeers: true, sidecar: true },
    });
    expect(networkViewFor('group-other', network)?.devices).toBeNull();
  });

  it('keeps failed and null panes unknown while retaining valid directory rows', () => {
    const partial: OrgNetworkResult = {
      ...network,
      mappedPeers: null,
      sidecar: null,
      paneStatus: {
        directory: { available: true },
        mappedPeers: { available: false, reason: 'timeout' },
        sidecar: { available: false, reason: 'timeout' },
      },
      directoryPending: { 'group-alpha': { contentPending: [{ owner: 'user:b', deviceId: 'device-b', reason: 'pending' }] } },
    };
    const view = networkViewFor('group-alpha', partial);
    expect(view?.devices).toHaveLength(1);
    expect(view?.mappedPeers).toBeNull();
    expect(view?.sidecar).toBeNull();
    expect(view?.warnings).toContain('部分设备记录仍在同步，以下列表可能不完整。');
  });

  it('does not label a null payload available even if the pane reports available', () => {
    const view = networkViewFor('group-alpha', { ...network, mappedPeers: null, sidecar: null });
    expect(view?.panes).toMatchObject({ directory: true, mappedPeers: false, sidecar: false });
  });

  it('keeps an unavailable organization directory unknown without static unavailable copy', () => {
    const view = networkViewFor('group-alpha', {
      ...network,
      directory: {},
      unavailableOrgs: [{ available: false, org: 'group-alpha', reason: 'policy-denied' }],
      paneStatus: { ...network.paneStatus!, directory: { available: false } },
    });
    expect(view?.panes.directory).toBe(false);
    expect(view?.devices).toBeNull();
    expect(view?.warnings).toEqual([]);
  });
});
