/**
 * Decision record 2026-11-10 (平台接入 UI 投影).
 *
 * - 列表按 provider 分类，条目只放摘要；具体操作与身份明细在条目详情里。
 * - 文案只做展示投影，不改变 API/daemon 状态。
 */
import {
  PLATFORM_PROVIDERS,
  type AdapterOperation,
  type PlatformIntegration,
} from './platform-integrations-api';

export interface PlatformIntegrationGroup {
  readonly provider: string;
  readonly rows: readonly PlatformIntegration[];
}

export function providerLabel(provider: string): string {
  return PLATFORM_PROVIDERS.find(item => item.id === provider)?.label ?? provider;
}

export function runtimeLabel(row: PlatformIntegration): string {
  if (!row.configured) return '未配置';
  if (row.online) return '在线';
  switch (row.status) {
    case 'starting': return '启动中';
    case 'error': return '异常';
    case 'quarantined': return '隔离';
    case 'stale': return '连接陈旧';
    case 'checking': return '检查中';
    case 'detached': return '已移除';
    default: return '离线';
  }
}

export function identityLabel(row: PlatformIntegration): string {
  if (row.identities.state === 'synced') {
    return `已同步 ${row.identities.count ?? 0} 个身份`;
  }
  if (row.identities.state === 'not-synced') return '身份未同步';
  return '身份状态未确认';
}

export function operationLabel(operation: AdapterOperation): string {
  switch (operation.status) {
    case 'starting': return '正在准备…';
    case 'awaiting_user': return '等待平台确认';
    case 'checking': return '正在核对结果…';
    case 'succeeded': return '已成功';
    case 'denied': return '已拒绝';
    case 'expired': return '已过期';
    case 'cancelled': return '已取消';
    case 'failed': return '失败';
    default: return '结果未确认';
  }
}

export function active(operation: AdapterOperation | undefined): boolean {
  return operation?.status === 'starting'
    || operation?.status === 'awaiting_user'
    || operation?.status === 'checking'
    || operation?.status === 'unknown';
}

export function groupByProvider(rows: readonly PlatformIntegration[]): readonly PlatformIntegrationGroup[] {
  const groups = new Map<string, PlatformIntegration[]>();
  for (const row of rows) {
    groups.set(row.provider, [...(groups.get(row.provider) ?? []), row]);
  }
  const order = new Map<string, number>(PLATFORM_PROVIDERS.map((item, index) => [item.id, index]));
  return [...groups.entries()]
    .sort(([left], [right]) => (
      (order.get(left) ?? Number.MAX_SAFE_INTEGER) - (order.get(right) ?? Number.MAX_SAFE_INTEGER)
    ))
    .map(([provider, items]) => ({ provider, rows: items }));
}
