import type { Dispatch } from 'react';
import { unsupportedNotice } from '../runtime/unsupported';
import type { GuiPage } from '../state/gui-view-model';
import type { AppAction } from '../state/types';
import type { DevModuleId } from '../dev-mode';
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
    case 'tasks':
      return 'tasks';
    case 'contacts':
      return 'agents';
    case 'org':
      return 'org';
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

interface RailProps {
  readonly page: GuiPage;
  readonly accountMenuOpen: boolean;
  readonly dispatch: Dispatch<AppAction>;
  readonly devLabels?: Readonly<Record<GuiPage, DevModuleId>>;
  readonly notifications: {
    readonly open: boolean;
    readonly unread: number;
    readonly items: readonly import('../state/types').NoticeItem[];
  };
}

export function Rail({
  page,
  accountMenuOpen,
  dispatch,
  devLabels,
  notifications,
}: RailProps) {
  return (
    <RailFrame
      active={railPage(page)}
      devLabels={
        devLabels
          ? {
              start: devLabels.start,
              chat: devLabels.chat,
              tasks: devLabels.tasks,
              agents: devLabels.contacts,
              org: devLabels.org,
            }
          : undefined
      }
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
          onNavigateConnections={() =>
            dispatch({ type: 'navigate', page: 'connections' })
          }
          onHelp={() =>
            dispatch({
              type: 'set-notice',
              text: unsupportedNotice('帮助中心'),
            })
          }
            onLogout={() => dispatch({ type: 'request-logout' })}
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
