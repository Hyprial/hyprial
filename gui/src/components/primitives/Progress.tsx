import { cx } from './types';

export interface ProgressBarProps {
  /** `null` means unknown. Unknown is never drawn as a completed bar. */
  readonly value: number | null;
  readonly max?: number;
  readonly label: string;
  readonly showValue?: boolean;
  readonly className?: string;
}

export function ProgressBar({
  value,
  max = 100,
  label,
  showValue = true,
  className,
}: ProgressBarProps) {
  const safeMax = Number.isFinite(max) && max > 0 ? max : 100;
  const knownValue =
    value === null || !Number.isFinite(value)
      ? null
      : Math.min(Math.max(value, 0), safeMax);
  const percent =
    knownValue === null ? null : Math.round((knownValue / safeMax) * 100);

  return (
    <div className={cx('ui-progress', className)}>
      <div className="ui-progress__meta">
        <span aria-hidden="true">{label}</span>
        {showValue && (
          <span className="ui-progress__value" aria-hidden="true">
            {percent === null ? '未知' : `${percent}%`}
          </span>
        )}
      </div>
      <div
        className="ui-progress__track"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={safeMax}
        aria-valuenow={knownValue ?? undefined}
        aria-valuetext={percent === null ? '未知' : `${percent}%`}
      >
        <span
          className="ui-progress__fill"
          data-unknown={percent === null ? 'true' : undefined}
          style={{ width: percent === null ? '0%' : `${percent}%` }}
        />
      </div>
    </div>
  );
}
