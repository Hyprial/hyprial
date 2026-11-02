import { useState } from 'react';
import { Button } from './Button';
import { cx } from './types';

export interface DiagnosticBoxProps {
  /** A safe, already-redacted diagnostic identifier; never a raw error dump. */
  readonly code: string;
  /** Text placed on the clipboard. Callers must pre-redact secrets and env data. */
  readonly copyText?: string;
  readonly title?: string;
  readonly description?: string;
  readonly className?: string;
}

type CopyState = 'idle' | 'copied' | 'failed';

export function DiagnosticBox({
  code,
  copyText,
  title = '诊断信息',
  description,
  className,
}: DiagnosticBoxProps) {
  const [copyState, setCopyState] = useState<CopyState>('idle');

  const handleCopy = async () => {
    const text = copyText ?? code;

    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
      setCopyState('failed');
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  };

  return (
    <section
      className={cx('ui-diagnostic', className)}
      aria-label={title}
    >
      <div className="ui-diagnostic__header">
        <p className="ui-diagnostic__title">{title}</p>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            void handleCopy();
          }}
        >
          复制诊断信息
        </Button>
      </div>
      {description && (
        <p className="ui-diagnostic__description">{description}</p>
      )}
      <code className="ui-diagnostic__code">{code}</code>
      <p
        className="ui-diagnostic__status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {copyState === 'copied' && '已复制'}
        {copyState === 'failed' && '无法自动复制，请手动选择诊断码'}
      </p>
    </section>
  );
}
