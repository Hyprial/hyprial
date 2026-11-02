import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Button as ShadcnButton } from '../ui/button';
import { cx } from './types';

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'ghost'
  | 'text'
  | 'danger';
export type ButtonSize = 'sm' | 'md';

const shadcnVariant = {
  primary: 'default',
  secondary: 'outline',
  ghost: 'ghost',
  text: 'link',
  danger: 'destructive',
} as const;

export interface ButtonProps
  extends Omit<
    ButtonHTMLAttributes<HTMLButtonElement>,
    'children' | 'onClick' | 'type'
  > {
  readonly variant?: ButtonVariant;
  readonly size?: ButtonSize;
  /** A busy button is also disabled so a slow action cannot be submitted twice. */
  readonly loading?: boolean;
  readonly type?: 'button' | 'submit' | 'reset';
  readonly leadingIcon?: ReactNode;
  readonly fullWidth?: boolean;
  readonly onClick?: () => void;
  readonly children: ReactNode;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  type = 'button',
  leadingIcon,
  fullWidth = false,
  onClick,
  children,
  className,
  disabled = false,
  ...rest
}: ButtonProps) {
  const handleClick = () => {
    if (loading || disabled) return;
    onClick?.();
  };

  return (
    <ShadcnButton
      {...rest}
      type={type}
      variant={shadcnVariant[variant]}
      size={size === 'md' ? 'default' : 'sm'}
      className={cx(
        'ui-button',
        `ui-button--${variant}`,
        `ui-button--${size}`,
        fullWidth && 'ui-button--full',
        loading && 'ui-button--loading',
        fullWidth && 'tw:w-full',
        loading && 'tw:disabled:cursor-progress',
        variant === 'text' && 'tw:min-h-0 tw:px-1',
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      onClick={handleClick}
    >
      {loading ? (
        <span className="ui-button__spinner" aria-hidden="true" />
      ) : (
        leadingIcon && (
          <span className="ui-button__icon" aria-hidden="true">
            {leadingIcon}
          </span>
        )
      )}
      <span className="ui-button__label">{children}</span>
    </ShadcnButton>
  );
}
