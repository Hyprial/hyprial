import type { WorkspaceApi } from '../workspace/api';
import type { OrgNetworkView } from '../domain/types';
import { OrgReadStateBlock, SectionHeader } from '../workspace/controls';

function networkTime(value: number | string | null): string {
  if (value === null) return '状态未确认';
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? '状态未确认' : time.toLocaleString('zh-CN');
}

function presenceLabel(value: string): string {
  if (value === 'online') return '在线';
  if (value === 'offline') return '离线';
  return '状态未确认';
}

function networkPane(label: string, ready: boolean): string {
  return `${label}：${ready ? '可用' : '状态未确认'}`;
}

export function NetworkStatusSection({ api }: { readonly api: WorkspaceApi }) {
  const { snapshot, detail } = api;
  if (snapshot.status === 'loading' || snapshot.status === 'error') {
    return (
      <div className="org-section" data-org-section="network-status">
        <SectionHeader title="网络状态" />
        <OrgReadStateBlock snapshot={snapshot} onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  const network: OrgNetworkView | null = detail?.network ?? null;
  return (
    <div className="org-section org-network-status" data-org-section="network-status">
      <SectionHeader
        title="网络状态"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <p className="org-scope-note">只读显示组织设备目录，以及本机的 Peer 映射与 Sidecar 状态。</p>
      <ul className="org-network-status__panes" aria-label="网络数据状态">
        <li>{networkPane('设备目录', network?.panes.directory === true)}</li>
        <li>{networkPane('Peer', network?.panes.mappedPeers === true)}</li>
        <li>{networkPane('Sidecar', network?.panes.sidecar === true)}</li>
      </ul>
      {network?.warnings.map((warning) => <p key={warning} className="org-unavailable" role="status">{warning}</p>)}
      <section className="org-network-status__pane" aria-labelledby="org-network-devices">
        <h3 id="org-network-devices">组织设备</h3>
        {network?.devices === null || network?.devices === undefined ? (
          <p>设备目录状态未确认</p>
        ) : network.devices.length === 0 ? (
          <p>设备目录暂无记录</p>
        ) : (
          <ul className="org-network-status__rows" aria-label="组织网络设备">
            {network.devices.map((device) => (
              <li key={device.id}>
                <div><strong>{device.name}</strong>{device.current ? <span className="org-tag org-tag--current">本机</span> : null}</div>
                <div className="org-network-status__meta">ID：{device.id} · 所有者：{device.owner} · {presenceLabel(device.presence)} · 更新于 {networkTime(device.updatedAt)}</div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="org-network-status__pane" aria-labelledby="org-network-peers">
        <h3 id="org-network-peers">已映射 Peer</h3>
        <p className="org-scope-note">此列表来自本机 Sidecar，可能包含其他组织的 Peer。</p>
        {network?.mappedPeers === null || network?.mappedPeers === undefined ? (
          <p>Peer 状态未确认</p>
        ) : network.mappedPeers.length === 0 ? (
          <p>暂无已映射 Peer</p>
        ) : (
          <ul className="org-network-status__rows" aria-label="已映射 Peer 列表">
            {network.mappedPeers.map((peer) => (
              <li key={peer.peer}>
                <strong>{network.selfDeviceId ? `${network.selfDeviceId} → ` : ''}{peer.peer}</strong>
                <div className="org-network-status__meta">地址：{peer.endpoint ?? '状态未确认'} · {peer.dialed ? '已拨号' : '未拨号'}</div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="org-network-status__pane" aria-labelledby="org-network-sidecar">
        <h3 id="org-network-sidecar">Sidecar 状态</h3>
        {network?.sidecar === null || network?.sidecar === undefined ? (
          <p>Sidecar 状态未确认</p>
        ) : (
          <dl className="org-network-status__facts">
            <div><dt>策略</dt><dd>{network.sidecar.policyMode ?? '状态未确认'}</dd></div>
            <div><dt>最近轮询</dt><dd>{networkTime(network.sidecar.lastPollAtMs)} · {network.sidecar.lastPollOk === null ? '状态未确认' : network.sidecar.lastPollOk ? '成功' : '失败'}</dd></div>
            <div><dt>可用槽位</dt><dd>{network.sidecar.slotsFree === null || network.sidecar.slotsTotal === null ? '状态未确认' : `${network.sidecar.slotsFree} / ${network.sidecar.slotsTotal}`}</dd></div>
            <div><dt>需要重启</dt><dd>{network.sidecar.restartRequired === null ? '状态未确认' : network.sidecar.restartRequired ? '是' : '否'}</dd></div>
          </dl>
        )}
      </section>

    </div>
  );
}
