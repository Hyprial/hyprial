import { useState } from 'react';
import { Button, Field } from '../primitives';

export interface SecretFieldProps {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly hint?: string;
  readonly autoComplete?: string;
  readonly disabled?: boolean;
  readonly required?: boolean;
  readonly showLabel?: string;
  readonly hideLabel?: string;
  /** Opt-in compact control; stacked remains the default for existing forms. */
  readonly layout?: 'stacked' | 'inline';
}

/**
 * A labeled secret input with an explicit reveal control.
 *
 * The value is controlled by the owning form; this component never writes it
 * to storage and never logs it. The caller owns clearing the value on submit,
 * cancel and unmount.
 */
export function SecretField({
  id,
  label,
  value,
  onChange,
  hint,
  autoComplete = 'off',
  disabled = false,
  required = false,
  showLabel = '显示内容',
  hideLabel = '隐藏内容',
  layout = 'stacked',
}: SecretFieldProps) {
  const [revealed, setRevealed] = useState(false);

  if (layout === 'inline') return (
    <div className="ui-field">
      <label className="ui-field__label" htmlFor={id}>{label}{required && <span className="ui-field__required" aria-hidden="true"> *</span>}</label>
      <div className="secret-field-inline">
        <input id={id} type={revealed ? 'text' : 'password'} value={value} autoComplete={autoComplete}
          disabled={disabled} required={required} aria-describedby={hint ? `${id}-hint` : undefined} spellCheck={false}
          onChange={(event) => onChange(event.target.value)} />
        <button type="button" className="secret-field-inline__reveal" disabled={disabled}
          aria-label={revealed ? hideLabel : showLabel} title={revealed ? hideLabel : showLabel}
          aria-pressed={revealed} aria-controls={id} onClick={() => setRevealed(current => !current)}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
            <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" />
            {revealed && <path d="m3 3 18 18" />}
          </svg>
        </button>
      </div>
      {hint && <p className="ui-field__hint" id={`${id}-hint`}>{hint}</p>}
    </div>
  );

  return (
    <>
      <Field label={label} htmlFor={id} hint={hint} required={required}>
        <input
          id={id}
          type={revealed ? 'text' : 'password'}
          value={value}
          autoComplete={autoComplete}
          disabled={disabled}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
      </Field>
      <div className="settings-actions">
        <Button
          variant="text"
          size="sm"
          aria-pressed={revealed}
          aria-controls={id}
          disabled={disabled}
          onClick={() => setRevealed((current) => !current)}
        >
          {revealed ? hideLabel : showLabel}
        </Button>
      </div>
    </>
  );
}
