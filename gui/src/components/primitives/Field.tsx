import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from 'react';
import { cx } from './types';

interface FieldControlProps {
  readonly 'aria-describedby'?: string;
  readonly 'aria-invalid'?: true;
  readonly 'aria-errormessage'?: string;
  readonly required?: boolean;
}

export interface FieldProps {
  readonly label: string;
  readonly htmlFor: string;
  readonly hint?: string;
  readonly error?: string;
  readonly required?: boolean;
  readonly children: ReactNode;
  readonly className?: string;
}

/**
 * Labels a single form control. When `children` is a single element, the
 * hint/error/required metadata is also applied to that element so assistive
 * technology gets the same association as the visible label. Secret lifecycle
 * stays with the owning form; this component never reads or stores values.
 */
export function Field({
  label,
  htmlFor,
  hint,
  error,
  required = false,
  children,
  className,
}: FieldProps) {
  const hintId = hint ? `${htmlFor}-hint` : undefined;
  const errorId = error ? `${htmlFor}-error` : undefined;
  const childProps = isValidElement<FieldControlProps>(children) ? children.props : undefined;
  const describedBy = [...new Set([
    ...(childProps?.['aria-describedby']?.split(/\s+/).filter(Boolean) ?? []),
    hintId,
    errorId,
  ].filter((id): id is string => Boolean(id)))].join(' ') || undefined;

  const control = childProps
    ? cloneElement(children as ReactElement<FieldControlProps>, {
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : childProps['aria-invalid'],
        'aria-errormessage': errorId ?? childProps['aria-errormessage'],
        required: required || childProps.required,
      })
    : children;

  return (
    <div className={cx('ui-field', error && 'ui-field--invalid', className)}>
      <label className="ui-field__label" htmlFor={htmlFor}>
        {label}
        {required && (
          <span className="ui-field__required" aria-hidden="true">
            {' '}
            *
          </span>
        )}
      </label>
      {control}
      {hint && (
        <p className="ui-field__hint" id={hintId}>
          {hint}
        </p>
      )}
      {error && (
        <p className="ui-field__error" id={errorId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
