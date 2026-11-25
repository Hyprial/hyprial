import { Icon, type IconName } from './primitives/Icon';

export type AccountIconName = 'settings' | 'connection' | 'logout';

const ACCOUNT_ICONS: Readonly<Record<AccountIconName, IconName>> = {
  settings: 'gear',
  connection: 'plugs-connected',
  logout: 'sign-out',
};

export function AccountIcon({ name }: { name: AccountIconName }) {
  return <Icon name={ACCOUNT_ICONS[name]} size="sm" className="menu-icon" />;
}
