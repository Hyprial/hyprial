import { useCallback, useId, type HTMLAttributes, type ReactNode, type Ref } from 'react';
import { Card as ShadcnCard } from '../ui/card';
import { Icon } from './Icon';
import { cn } from '@/lib/utils';
import { cx, type UiTone } from './types';

export interface CardProps
  extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
  readonly as?: 'div' | 'section' | 'article' | 'aside';
  readonly tone?: UiTone;
  readonly padding?: 'sm' | 'md' | 'lg';
  readonly interactive?: boolean;
  readonly selected?: boolean;
  readonly 'data-state'?: string;
  readonly children: ReactNode;
  /** 允许调用方持有 surface 元素（例如焦点回归）。 */
  readonly ref?: Ref<HTMLElement | null>;
}

/** A surface only. Business meaning and loading/error decisions stay upstream. */
export function Card({
  as: Tag = 'div',
  tone = 'neutral',
  padding = 'md',
  interactive = false,
  selected = false,
  children,
  className,
  ref: elementRef,
  ...rest
}: CardProps) {
  // 安全转发：函数 ref 直接用，对象 ref 用赋值，避免不真实的 HTMLDivElement 断言。
  const assignRef = useCallback(
    (node: HTMLElement | null) => {
      if (!elementRef) return;
      if (typeof elementRef === 'function') {
        elementRef(node);
        return;
      }
      (elementRef as { current: HTMLElement | null }).current = node;
    },
    [elementRef],
  );
  return (
    <ShadcnCard
      asChild
      className={cn(
        'ui-card',
        `ui-card--${tone}`,
        `ui-card--${padding}`,
        interactive && 'ui-card--interactive',
        selected && 'ui-card--selected',
        { sm: 'tw:p-3', md: 'tw:p-5', lg: 'tw:p-6' }[padding],
        {
          neutral: '',
          info: 'tw:bg-[var(--ui-info-wash)]',
          positive: 'tw:bg-[var(--ui-positive-wash)]',
          warning: 'tw:bg-[var(--ui-warning-wash)]',
          danger: 'tw:bg-[var(--ui-danger-wash)]',
        }[tone],
        interactive && 'tw:cursor-pointer tw:transition-[background-color,box-shadow] tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:hover:bg-[var(--ui-surface-hover)] tw:motion-reduce:transition-none',
        selected && 'tw:bg-[var(--ui-neutral-fill)] tw:shadow-[inset_0_0_0_2px_var(--ui-ink-1)]',
        className,
      )}
    >
      <Tag ref={assignRef} {...rest} data-selected={selected ? 'true' : undefined}>{children}</Tag>
    </ShadcnCard>
  );
}

export type StatusCardState = 'pending' | 'running' | 'done' | 'failed';

export interface StatusCardProps {
  readonly label: string;
  /** `null` means the source did not provide a value; it is never rendered as zero. */
  readonly value: string | null;
  readonly hint?: string;
  readonly tone?: UiTone;
  /**
   * Opt-in: only the first-run steps pass a state. Callers that omit it keep the
   * previous rendering exactly — no icon, no details, hint stays inline.
   */
  readonly state?: StatusCardState;
  readonly className?: string;
  readonly id?: string;
}

export function StatusCard({
  label,
  value,
  hint,
  tone = 'neutral',
  state,
  className,
  id,
}: StatusCardProps) {
  const displayValue = value ?? '未提供';
  const generatedId = useId().replace(/:/g, '');
  const detailsId = id ? `${id}-details` : `ui-status-card-${generatedId}-details`;
  const showDetails = state === 'failed' && Boolean(hint);

  return (
    <Card
      as="article"
      tone={tone}
      padding="md"
      className={cx('ui-status-card', 'tw:p-4', tone === 'neutral' && 'tw:bg-[var(--ui-ground-2)]', className)}
      aria-label={`${label}：${displayValue}`}
      id={id}
      data-state={state}
      aria-describedby={showDetails ? detailsId : undefined}
    >
      <p className="ui-status-card__label">{label}</p>
      <p
        className="ui-status-card__value"
        data-unknown={value === null ? 'true' : undefined}
      >
        {state && (
          <span
            className="ui-status-card__icon"
            data-state={state}
            aria-hidden="true"
          >
            {state === 'running' && <Icon name="arrow-clockwise" size="sm" />}
            {state === 'done' && <Icon name="check-circle" size="sm" active />}
            {state === 'failed' && <Icon name="warning-circle" size="sm" active />}
          </span>
        )}
        {displayValue}
      </p>
      {hint && !showDetails && <p className="ui-status-card__hint">{hint}</p>}
      {showDetails && (
        <details className="ui-status-card__details" id={detailsId} open>
          <summary>查看说明</summary>
          <p className="ui-status-card__hint">{hint}</p>
        </details>
      )}
    </Card>
  );
}
