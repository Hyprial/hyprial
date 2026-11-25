import type { Dispatch } from 'react';
import type { GuiPage } from '../state/gui-view-model';
import type { AppAction } from '../state/types';
import { AccountMenu } from './AccountMenu';
import { NotificationCenter } from './NotificationCenter';
import './notification.css';
import { RailFrame, type RailPage } from './RailFrame';

function railPage(page: GuiPage): RailPage | null {
  switch (page) {
    case 'start':
      return 'start';
    case 'chat':
      return 'chat';
    case 'contacts':
      return 'agents';
    case 'org':
      return 'org';
    case 'device':
      return 'device';
    case 'connections':
    case 'settings':
    case 'first-run':
    default:
      return null;
  }
}

function guiPage(page: RailPage): GuiPage {
  return page === 'agents' ? 'contacts' : page;
}

function railHiddenPages(hidden: ReadonlySet<GuiPage> | undefined): ReadonlySet<RailPage> | undefined {
  if (hidden === undefined || hidden.size === 0) return undefined;
  const pages = new Set<RailPage>();
  for (const page of hidden) {
    const target = railPage(page);
    if (target !== null) pages.add(target);
  }
  return pages;
}

interface RailProps {
  readonly page: GuiPage;
  readonly accountMenuOpen: boolean;
  readonly authenticated?: boolean;
  readonly hiddenPages?: ReadonlySet<GuiPage>;
  readonly dispatch: Dispatch<AppAction>;
  readonly notifications: {
    readonly open: boolean;
    readonly unread: number;
    readonly items: readonly import('../state/types').NoticeItem[];
  };
}

export function Rail({
  page,
  accountMenuOpen,
  authenticated = false,
  hiddenPages,
  dispatch,
  notifications,
}: RailProps) {
  return (
    <RailFrame
      active={railPage(page)}
      hiddenPages={railHiddenPages(hiddenPages)}
      onSelect={(target) =>
        dispatch({ type: 'navigate', page: guiPage(target) })
      }
      footer={
        <div className="rail-foot">
          <AccountMenu
          open={accountMenuOpen}
          onOpenChange={(open) =>
            dispatch({ type: 'toggle-account-menu', open })
          }
          onNavigateSettings={() =>
            dispatch({ type: 'navigate', page: 'settings' })
          }
          onNavigateConnections={hiddenPages?.has('connections') ? undefined : () =>
            dispatch({ type: 'navigate', page: 'connections' })
          }
            onLogout={authenticated ? () => dispatch({ type: 'request-logout' }) : undefined}
          />
          <NotificationCenter
            open={notifications.open}
            unread={notifications.unread}
            items={notifications.items}
            dispatch={dispatch}
          />
        </div>
      }
    />
  );
}
