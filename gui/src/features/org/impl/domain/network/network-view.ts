import type { OrgNetworkResult } from '../../../../../client/generated/types';
import type { OrgNetworkView } from '../types';

export function networkViewFor(org: string, network: OrgNetworkResult | null): OrgNetworkView | null {
  if (network === null) return null;
  const directoryRows = network.directory?.[org];
  const panes = {
    directory: network.paneStatus?.directory.available === true && Array.isArray(directoryRows),
    mappedPeers: network.paneStatus?.mappedPeers.available === true && Array.isArray(network.mappedPeers),
    sidecar: network.paneStatus?.sidecar.available === true && network.sidecar != null,
  };
  const pending = network.directoryPending?.[org];
  return {
    panes,
    devices: panes.directory && Array.isArray(directoryRows)
      ? directoryRows.map((row) => ({
          id: row.deviceId,
          name: row.machineFingerprint || row.deviceId,
          owner: row.owner,
          presence: row.presence,
          updatedAt: row.updatedAt,
          current: network.self?.deviceId === row.deviceId,
        }))
      : null,
    mappedPeers: panes.mappedPeers && Array.isArray(network.mappedPeers)
      ? network.mappedPeers.map(({ peer, endpoint, dialed }) => ({ peer, endpoint, dialed }))
      : null,
    sidecar: panes.sidecar && network.sidecar
      ? {
          policyMode: network.sidecar.policy?.mode ?? null,
          lastPollAtMs: network.sidecar.lastPoll?.atMs ?? null,
          lastPollOk: network.sidecar.lastPoll?.ok ?? null,
          slotsFree: network.sidecar.slots?.free ?? null,
          slotsTotal: network.sidecar.slots?.poolSize ?? null,
          restartRequired: network.sidecar.restartRequired ?? null,
        }
      : null,
    warnings: [
      ...((network.dataProblems ?? []).some((item) => item.org === org)
        ? ['设备目录存在数据问题，以下列表可能不完整。'] : []),
      ...(pending && pending.contentPending.length > 0
        ? ['部分设备记录仍在同步，以下列表可能不完整。'] : []),
    ],
    selfDeviceId: network.self?.deviceId ?? null,
  };
}
