import type { ReactNode } from 'react';
import { Button } from './Button';
import { cx, type UiTone } from './types';

export interface NoticeAction {
  readonly label: string;
  readonly onClick: () => void;
  readonly disabled?: boolean;
}

export interface NoticeProps {
  readonly tone?: UiTone;
  readonly title?: string;
  readonly children: ReactNode;
  readonly action?: NoticeAction;
  readonly onDismiss?: () => void;
  readonly dismissLabel?: string;
  readonly role?: 'status' | 'alert';
  readonly className?: string;
}

/** Inline status/alert copy with an optional explicit recovery action. */
export function Notice({
  tone = 'info',
  title,
  children,
  action,
  onDismiss,
  dismissLabel = '关闭提示',
  role,
  className,
}: NoticeProps) {
  const resolvedRole =
    role ?? (tone === 'danger' ? 'alert' : ('status' as const));

  return (
    <div
      className={cx('ui-notice', `ui-notice--${tone}`, className)}
      role={resolvedRole}
      aria-live={resolvedRole === 'alert' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <span className="ui-notice__marker" aria-hidden="true" />
      <div className="ui-notice__content">
        {title && <p className="ui-notice__title">{title}</p>}
        <div className="ui-notice__body">{children}</div>
        {action && (
          <div className="ui-notice__action">
            <Button
              variant="secondary"
              size="sm"
              disabled={action.disabled}
              onClick={action.onClick}
            >
              {action.label}
            </Button>
          </div>
        )}
      </div>
      {onDismiss && (
        <button
          type="button"
          className="ui-notice__dismiss"
          aria-label={dismissLabel}
          onClick={onDismiss}
        >
          <span aria-hidden="true">×</span>
        </button>
      )}
    </div>
  );
}
