import { useGuiCapabilities } from '../app/capabilities';
import { PageHeader } from '../components/primitives';
import { useAccountSettings, AccountLoginSection } from '../features/account';

export function SettingsPage() {
  const { firstRun: actions } = useGuiCapabilities();
  const accountSettings = useAccountSettings(actions);
  return <section className="page page-settings" data-page="settings">
    <PageHeader title="用户设置" />
    <div className="settings-stack">
      <AccountLoginSection state={accountSettings} onOpenAuthorization={actions.openAuthorization} />
    </div>
  </section>;
}
