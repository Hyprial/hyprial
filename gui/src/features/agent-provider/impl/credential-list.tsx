/** Shared credential list layout; disclosure state is presentation only. */
import { useId, useState, type ReactNode } from 'react';
import { Button } from '../../../components/primitives';
import './credential-list.css';

export function CredentialList({ label, children }: { readonly label: string; readonly children: ReactNode }) {
  return <ul className="credentials-list" aria-label={label}>{children}</ul>;
}

export function CredentialListItem({ title, summary, children, expanded, onExpandedChange, defaultExpanded = false }: {
  readonly title: ReactNode;
  readonly summary: ReactNode;
  readonly children: ReactNode;
  readonly expanded?: boolean;
  readonly onExpandedChange?: (expanded: boolean) => void;
  readonly defaultExpanded?: boolean;
}) {
  const [localExpanded, setLocalExpanded] = useState(defaultExpanded);
  const open = expanded ?? localExpanded;
  const id = useId();
  return (
    <li className="credentials-item">
      <div className="credentials-row">
        <div className="credentials-row__main">
          <strong className="credentials-row__name">{title}</strong>
          <span className="credentials-row__meta">{summary}</span>
        </div>
        <Button variant="secondary" aria-expanded={open} aria-controls={id} onClick={() => {
          setLocalExpanded(!open);
          onExpandedChange?.(!open);
        }}>{open ? '收起' : '查看详情'}</Button>
      </div>
      {open && <div className="credentials-detail" id={id}>{children}</div>}
    </li>
  );
}
