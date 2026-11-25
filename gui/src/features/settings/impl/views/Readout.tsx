import type { ReactNode } from 'react';
import { Button, Icon } from '../../../../components/primitives';

export function Readout({ rows }: { readonly rows: readonly (readonly [string, ReactNode])[] }) {
  return <dl className="settings-readout">{rows.map(([label, value]) => <div key={label}>
    <dt>{label}</dt><dd>{value ?? '—'}</dd>
  </div>)}</dl>;
}

export function RefreshButton({ label, disabled, onClick }: {
  readonly label: string; readonly disabled: boolean; readonly onClick: () => void;
}) {
  return <Button variant="text" size="compact" aria-label={`刷新${label}`} leadingIcon={<Icon name="arrow-clockwise" />}
    disabled={disabled} onClick={onClick}>刷新</Button>;
}

export function readoutTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}
