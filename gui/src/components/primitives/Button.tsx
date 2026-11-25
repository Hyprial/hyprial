import { forwardRef, type ButtonHTMLAttributes, type MouseEventHandler, type ReactNode } from 'react';
import { Button as ShadcnButton } from '../ui/button';
import { cx } from './types';

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'ghost'
  | 'text'
  | 'danger';
export type ButtonSize = 'sm' | 'md' | 'compact';

/** compact：文本动作专用（无边框/无内距/12px），与 onboarding 既有 text 按钮区分。 */
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
  readonly onClick?: MouseEventHandler<HTMLButtonElement>;
  readonly children: ReactNode;
}

/**
 * 共享按钮。forwardRef 让需要焦点管理等 ref 的场景（对话框、删除确认）也能使用同一组件，
 * 不必保留 raw <button> 与 legacy class。
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
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
}: ButtonProps, ref) {
  const handleClick: MouseEventHandler<HTMLButtonElement> = (event) => {
    if (loading || disabled) return;
    onClick?.(event);
  };

  return (
    <ShadcnButton
      {...rest}
      ref={ref}
      type={type}
      variant={shadcnVariant[variant]}
      size={size === 'md' ? 'default' : size === 'compact' ? 'sm' : 'sm'}
      className={cx(
        'ui-button',
        `ui-button--${variant}`,
        `ui-button--${size}`,
        size === 'compact' && 'ui-button--compact',
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
});
