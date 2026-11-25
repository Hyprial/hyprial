/** Shared device page: one setup action, then account-scoped read-only status. */
import { PageHeader } from '../components/primitives';
import { useGuiCapabilities } from '../app/capabilities';
import { DeviceStatusSection, DiagnosticSection, LogsSection, UpgradeStatusSection } from '../features/settings';

export function DevicePage() {
  const { firstRun, localManagement, upgradeStatus, diagnostic, logs } = useGuiCapabilities();
  return <section className="page page-settings" data-page="device">
    <PageHeader title="设备" />
    <div className="settings-stack">
      <DeviceStatusSection actions={firstRun} api={localManagement}>
        {upgradeStatus && <UpgradeStatusSection api={upgradeStatus} />}
        {diagnostic && <DiagnosticSection api={diagnostic} />}
        {logs && <LogsSection api={logs} />}
      </DeviceStatusSection>
    </div>
  </section>;
}
