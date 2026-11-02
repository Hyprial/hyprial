import { Button } from './Button';
import { cx, type UiAction } from './types';

interface StateFrameProps {
  readonly tone: 'neutral' | 'info' | 'warning' | 'danger';
  readonly mark: string;
  readonly title: string;
  readonly description?: string;
  readonly code?: string;
  readonly action?: UiAction;
  readonly busy?: boolean;
  readonly role?: 'status' | 'alert';
  readonly className?: string;
}

function StateFrame({
  tone,
  mark,
  title,
  description,
  code,
  action,
  busy = false,
  role = 'status',
  className,
}: StateFrameProps) {
  return (
    <div
      className={cx('ui-state', `ui-state--${tone}`, className)}
      role={role}
      aria-live={role === 'alert' ? 'assertive' : 'polite'}
      aria-busy={busy || undefined}
    >
      <span className="ui-state__mark" aria-hidden="true">
        {busy ? <span className="ui-state__spinner" /> : mark}
      </span>
      <p className="ui-state__title">{title}</p>
      {description && <p className="ui-state__description">{description}</p>}
      {code && <code className="ui-state__code">{code}</code>}
      {action && (
        <Button
          variant="secondary"
          size="sm"
          disabled={action.disabled}
          onClick={action.onClick}
        >
          {action.label}
        </Button>
      )}
    </div>
  );
}

export interface LoadingStateProps {
  readonly title?: string;
  readonly description?: string;
  readonly className?: string;
}

export function LoadingState({
  title = '正在加载',
  description,
  className,
}: LoadingStateProps) {
  return (
    <StateFrame
      tone="info"
      mark="…"
      title={title}
      description={description}
      busy
      className={className}
    />
  );
}

export interface EmptyStateProps {
  readonly title?: string;
  readonly description?: string;
  readonly action?: UiAction;
  readonly className?: string;
}

export function EmptyState({
  title = '暂无内容',
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <StateFrame
      tone="neutral"
      mark="—"
      title={title}
      description={description}
      action={action}
      className={className}
    />
  );
}

export interface UnsupportedStateProps {
  readonly title?: string;
  readonly reason: string;
  readonly code?: string;
  readonly action?: UiAction;
  readonly className?: string;
}

export function UnsupportedState({
  title = '暂不支持',
  reason,
  code,
  action,
  className,
}: UnsupportedStateProps) {
  return (
    <StateFrame
      tone="warning"
      mark="!"
      title={title}
      description={reason}
      code={code}
      action={action}
      className={className}
    />
  );
}

export interface ErrorStateProps {
  readonly title?: string;
  readonly message: string;
  readonly retryable?: boolean;
  readonly code?: string;
  readonly retryLabel?: string;
  readonly onRetry?: () => void;
  readonly className?: string;
}

export function ErrorState({
  title = '加载失败',
  message,
  retryable = false,
  code,
  retryLabel = '重试',
  onRetry,
  className,
}: ErrorStateProps) {
  const action =
    retryable && onRetry
      ? { label: retryLabel, onClick: onRetry }
      : undefined;

  return (
    <StateFrame
      tone="danger"
      mark="×"
      title={title}
      description={message}
      code={code}
      action={action}
      role="alert"
      className={className}
    />
  );
}
