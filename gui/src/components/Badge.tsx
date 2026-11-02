import type {
  ConnectionSummary,
  TargetAvailability,
} from '../state/gui-view-model';
import { AVAILABILITY_LABELS } from '../state/selectors';

export function Badge({
  children,
  tone = 'neutral',
}: {
  readonly children: string;
  readonly tone?: 'positive' | 'warning' | 'muted' | 'neutral';
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function AvailabilityBadge({
  availability,
}: {
  readonly availability: TargetAvailability;
}) {
  const tone =
    availability === 'available'
      ? 'positive'
      : availability === 'needs-connection'
        ? 'warning'
        : 'muted';
  return <Badge tone={tone}>{AVAILABILITY_LABELS[availability]}</Badge>;
}

const CONNECTION_STATUS_LABELS: Readonly<
  Record<ConnectionSummary['status'], string>
> = {
  connected: '已连接',
  unconfigured: '未配置',
  disabled: '已停用',
};

export function ConnectionStatusBadge({
  status,
}: {
  readonly status: ConnectionSummary['status'];
}) {
  const tone =
    status === 'connected'
      ? 'positive'
      : status === 'unconfigured'
        ? 'warning'
        : 'muted';
  return <Badge tone={tone}>{CONNECTION_STATUS_LABELS[status]}</Badge>;
}
