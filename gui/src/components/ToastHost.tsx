import { useEffect } from 'react';
import type { Dispatch } from 'react';
import type { AppAction } from '../state/types';
import type { NoticeItem } from '../state/types';
import { Icon, type IconName } from './primitives/Icon';

/**
 * Unified transient notification surface.
 *
 * Every action result flows through here as a stacked toast. Info/success
 * auto-dismiss; warning/error stay until dismissed unless the notice sets
 * autoDismiss, and are mirrored into the notification-center history by the
 * reducer. The container is the polite
 * live region (role=status); individual toasts carry no role. The host sits
 * above modal dialogs (--ui-z-toast) so a toast raised from a dialog is seen.
 */

const AUTO_DISMISS_MS = 4000;

const TONE_ICONS: Readonly<Record<NoticeItem['tone'], IconName>> = {
  info: 'info',
  success: 'check-circle',
  warning: 'warning',
  error: 'warning-circle',
};

const TONE_LABELS: Readonly<Record<NoticeItem['tone'], string>> = {
  info: '提示',
  success: '成功',
  warning: '注意',
  error: '错误',
};

function Toast({
  notice,
  onDismiss,
}: {
  readonly notice: NoticeItem;
  readonly onDismiss: () => void;
}) {
  const auto = notice.autoDismiss ?? (notice.tone === 'info' || notice.tone === 'success');

  useEffect(() => {
    if (!auto) {
      return undefined;
    }
    const timer = window.setTimeout(onDismiss, AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [auto, onDismiss, notice.id]);

  return (
    <div className={`toast toast-${notice.tone}`}>
      <span className="toast-tone" aria-hidden="true">
        <Icon name={TONE_ICONS[notice.tone]} size="sm" />
        {TONE_LABELS[notice.tone]}
      </span>
      <span className="toast-text">{notice.text}</span>
      <button
        type="button"
        className="toast-close"
        aria-label="关闭提示"
        onClick={onDismiss}
      >
        <Icon name="x" size="sm" />
      </button>
    </div>
  );
}

export function ToastHost({
  notices,
  dispatch,
}: {
  readonly notices: readonly NoticeItem[];
  readonly dispatch: Dispatch<AppAction>;
}) {
  if (notices.length === 0) {
    return null;
  }
  return (
    <div className="toast-host" role="status" aria-live="polite" aria-atomic="false">
      {notices.map((notice) => (
        <Toast
          key={notice.id}
          notice={notice}
          onDismiss={() => dispatch({ type: 'dismiss-notice', id: notice.id })}
        />
      ))}
    </div>
  );
}
