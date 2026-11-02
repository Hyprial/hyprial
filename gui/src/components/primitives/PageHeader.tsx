import type { ReactNode } from 'react';
import { cx } from './types';

export interface PageHeaderProps {
  readonly title: string;
  readonly eyebrow?: string;
  readonly description?: string;
  readonly actions?: ReactNode;
  readonly stepper?: ReactNode;
  readonly headingLevel?: 1 | 2 | 3;
  readonly className?: string;
}

export function PageHeader({
  title,
  eyebrow,
  description,
  actions,
  stepper,
  headingLevel = 1,
  className,
}: PageHeaderProps) {
  const Heading = `h${headingLevel}` as 'h1' | 'h2' | 'h3';

  return (
    <header className={cx('ui-page-header', className)}>
      <div className="ui-page-header__text">
        {eyebrow && <p className="ui-page-header__eyebrow">{eyebrow}</p>}
        <Heading className="ui-page-header__title">{title}</Heading>
        {description && (
          <p className="ui-page-header__description">{description}</p>
        )}
      </div>
      {actions && <div className="ui-page-header__actions">{actions}</div>}
      {stepper && <div className="ui-page-header__stepper">{stepper}</div>}
    </header>
  );
}
