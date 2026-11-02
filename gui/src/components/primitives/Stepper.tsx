import { cx } from './types';

export type StepStatus =
  | 'pending'
  | 'active'
  | 'completed'
  | 'failed'
  | 'blocked';

export interface StepDefinition {
  readonly id: string;
  readonly label: string;
  readonly status: StepStatus;
}

export interface StepperProps {
  readonly steps: ReadonlyArray<StepDefinition>;
  readonly currentId: string;
  readonly ariaLabel?: string;
  readonly className?: string;
}

const STATUS_LABELS: Readonly<Record<StepStatus, string>> = {
  pending: '待处理',
  active: '进行中',
  completed: '已完成',
  failed: '失败',
  blocked: '已阻塞',
};

const STATUS_MARKS: Readonly<Record<StepStatus, string>> = {
  pending: '·',
  active: '›',
  completed: '✓',
  failed: '×',
  blocked: '!',
};

/** Display-only progress. Callers own step transitions and the current step. */
export function Stepper({
  steps,
  currentId,
  ariaLabel = '步骤',
  className,
}: StepperProps) {
  return (
    <nav className={cx('ui-stepper', className)} aria-label={ariaLabel}>
      <ol className="ui-stepper__list">
        {steps.map((step) => {
          const isCurrent = step.id === currentId;
          return (
            <li
              key={step.id}
              className="ui-step"
              data-status={step.status}
              aria-current={isCurrent ? 'step' : undefined}
            >
              <span className="ui-step__marker" aria-hidden="true">
                {STATUS_MARKS[step.status]}
              </span>
              <span className="ui-step__label">{step.label}</span>
              <span className="ui-visually-hidden">
                （{STATUS_LABELS[step.status]}）
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
