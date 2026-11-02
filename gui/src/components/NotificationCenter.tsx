import type { Dispatch } from 'react';
import type { AppAction, NoticeItem } from '../state/types';

/**
 * Persistent notification center: warning/error toasts land here as history.
 *
 * The event feed itself is not wired yet (no backend); until then the list
 * only mirrors local outcomes and an honest empty state. Design reserves this
 * surface for the 0.5.5.3 event vocabulary (provider.grant.blocked{code},
 * turn.launch.refused, org.invite.pending, org.admission.refused, …).
 */
export function NotificationCenter({
  open,
  unread,
  items,
  dispatch,
}: {
  readonly open: boolean;
  readonly unread: number;
  readonly items: readonly NoticeItem[];
  readonly dispatch: Dispatch<AppAction>;
}) {
  return (
    <div className="notif">
      <button
        type="button"
        className="notif-bell"
        aria-label={
          unread > 0 ? `通知中心，${unread} 条未读` : '通知中心'
        }
        aria-expanded={open}
        onClick={() =>
          dispatch({ type: 'toggle-notification-center', open: !open })
        }
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          className="notif-bell-icon"
        >
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {unread > 0 && <span className="notif-dot" aria-hidden="true" />}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="通知中心">
          <div className="notif-head">
            <strong>通知中心</strong>
            {items.length > 0 && (
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  dispatch({ type: 'clear-center-notifications' })
                }
              >
                清空
              </button>
            )}
          </div>
          {items.length === 0 ? (
            <p className="notif-empty" role="status">
              暂无通知，状态未确认。
            </p>
          ) : (
            <ul className="notif-list">
              {items.map((item) => (
                <li key={item.id} className={`notif-item notif-${item.tone}`}>
                  <span className="notif-text">{item.text}</span>
                  <span className="notif-source">
                    {item.source ? `来源 · ${item.source}` : '来源未确认'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
