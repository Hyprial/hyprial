import type { Dispatch } from 'react';
import type { StubOperations } from '../adapters/stub';
import { useGuiCapabilities } from '../app/capabilities';
import { Button, Card, PageHeader, StatusCard } from '../components/primitives';
import { useAccountSettings } from '../features/account/useAccountSettings';
import { AccountLoginSection } from '../features/account/AccountLoginSection';
import { ModelCredentialSettings } from '../features/model/ModelCredentialSettings';
import { AgentProviderSection } from '../features/agent-provider/AgentProviderSection';
import {
  devicePresentation,
  networkPresentation,
} from '../features/onboarding/presentation';
import type { AppAction } from '../state/types';

interface SettingsPageProps { readonly dispatch: Dispatch<AppAction>; readonly stub: StubOperations; }

export function SettingsPage({ dispatch }: SettingsPageProps) {
  const { firstRun: actions, accountLoginDisabled, agentProvider, agentProviderLoginFiles } = useGuiCapabilities();
  const accountSettings = useAccountSettings(actions);
  const { snapshot, busy, run } = accountSettings;
  const authenticated = snapshot.account.status === 'authenticated';
  const device = devicePresentation({ kind: 'settled', result: snapshot.device }, authenticated, 'live');
  const network = networkPresentation();
  return <section className="page page-settings" data-page="settings">
    <PageHeader eyebrow="系统" title="设置" description="管理当前实例的账号、设备和模型凭证。与首次设置使用同一套能力。" />
    <div className="settings-grid">
      <AccountLoginSection state={accountSettings} disabled={accountLoginDisabled} />
      <Card as="section" className="settings-card">
        <h2>当前设备</h2>
        <StatusCard label="设备状态" {...device} />
        <StatusCard label="网络" {...network} />
        {snapshot.device.message && <p>{snapshot.device.message}</p>}
        {authenticated && snapshot.device.status !== 'ready' && <Button disabled={busy !== null} onClick={() => void run('device')}>初始化当前设备</Button>}
        <p className="settings-footnote">设备身份由当前应用实例提供，不从其他安装实例复制。</p>
      </Card>
      <Card as="section" className="settings-card settings-card-wide">
        <h2>模型服务</h2>
        <ModelCredentialSettings actions={actions} />
        <AgentProviderSection api={agentProvider} loginFiles={agentProviderLoginFiles} />
      </Card>
      <Card as="section" className="settings-card settings-card-wide">
        <h2>首次设置与普通偏好</h2>
        <p>需要继续准备 Squire 时可重新进入首次设置，已有状态会从当前实例恢复。</p>
        <Button onClick={() => dispatch({ type: 'navigate', page: 'first-run' })}>打开首次设置</Button>
        <StatusCard label="偏好设置" value="未提供 · 未接线" tone="neutral" hint="偏好持久化尚未接入，不显示虚构保存状态。" />
      </Card>
    </div>
  </section>;
}
