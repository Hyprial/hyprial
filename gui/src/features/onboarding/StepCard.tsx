import { useId, type ReactNode } from 'react';
import { Card, cx } from '../../components/primitives';
import type { FirstRunStepId, StepViewState } from './types';

interface StepCardProps {
  readonly id: FirstRunStepId;
  readonly title: string;
  readonly state: StepViewState;
  readonly children: ReactNode;
}

/**
 * A shared surface only. The parent owns step transitions; this wrapper adds
 * stable data attributes for private tests and responsive screenshots.
 */
export function StepCard({
  id,
  title,
  state,
  children,
}: StepCardProps) {
  const generatedId = useId().replace(/:/g, '');
  const headingId = `dev01-onboarding-${generatedId}-${id}`;

  return (
    <section
      className="dev01-v17-step-panel"
      data-step={id}
      data-state={state}
      data-active={state === 'active' ? 'true' : undefined}
      data-completed={state === 'completed' ? 'true' : undefined}
      data-locked={state === 'locked' ? 'true' : undefined}
      aria-labelledby={headingId}
    >
      <header className="dev01-v17-step-header">
        <h2 id={headingId} tabIndex={-1}>
          {title}
        </h2>
      </header>
      <Card as="div" className={cx('first-run-card', 'dev01-v17-card')}>
        {children}
      </Card>
    </section>
  );
}
