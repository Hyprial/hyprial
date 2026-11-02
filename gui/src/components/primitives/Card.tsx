import type { HTMLAttributes, ReactNode } from 'react';
import { Card as ShadcnCard } from '../ui/card';
import { cn } from '@/lib/utils';
import { cx, type UiTone } from './types';

export interface CardProps
  extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
  readonly as?: 'div' | 'section' | 'article' | 'aside';
  readonly tone?: UiTone;
  readonly padding?: 'sm' | 'md' | 'lg';
  readonly interactive?: boolean;
  readonly selected?: boolean;
  readonly children: ReactNode;
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
  ...rest
}: CardProps) {
  return (
    <ShadcnCard
      asChild
      className={cn(
        'ui-card',
        `ui-card--${tone}`,
        `ui-card--${padding}`,
        interactive && 'ui-card--interactive',
        selected && 'ui-card--selected',
        { sm: 'tw:p-3', md: 'tw:p-4', lg: 'tw:p-6' }[padding],
        {
          neutral: '',
          info: 'tw:border-[var(--ui-info)] tw:bg-[var(--ui-info-wash)]',
          positive: 'tw:border-[var(--ui-positive)] tw:bg-[var(--ui-positive-wash)]',
          warning: 'tw:border-[var(--ui-warning)] tw:bg-[var(--ui-warning-wash)]',
          danger: 'tw:border-[var(--ui-danger)] tw:bg-[var(--ui-danger-wash)]',
        }[tone],
        interactive && 'tw:cursor-pointer tw:transition-[border-color,box-shadow] tw:duration-[var(--ui-motion-fast)] tw:ease-[var(--ui-ease-standard)] tw:hover:border-primary tw:motion-reduce:transition-none',
        selected && 'tw:border-primary tw:shadow-[0_0_0_1px_var(--ui-accent)]',
        className,
      )}
    >
      <Tag {...rest} data-selected={selected ? 'true' : undefined}>{children}</Tag>
    </ShadcnCard>
  );
}

export interface StatusCardProps {
  readonly label: string;
  /** `null` means the source did not provide a value; it is never rendered as zero. */
  readonly value: string | null;
  readonly hint?: string;
  readonly tone?: UiTone;
  readonly className?: string;
}

export function StatusCard({
  label,
  value,
  hint,
  tone = 'neutral',
  className,
}: StatusCardProps) {
  const displayValue = value ?? '未提供';

  return (
    <Card
      as="article"
      tone={tone}
      padding="md"
      className={cx('ui-status-card', className)}
      aria-label={`${label}：${displayValue}`}
    >
      <p className="ui-status-card__label">{label}</p>
      <p
        className="ui-status-card__value"
        data-unknown={value === null ? 'true' : undefined}
      >
        {displayValue}
      </p>
      {hint && <p className="ui-status-card__hint">{hint}</p>}
    </Card>
  );
}
