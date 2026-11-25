import { Card, Notice } from '../../../components/primitives';
import type { LocalManagementApi } from './api';
import { useLocalManagement } from './useLocalManagement';
import { Readout, RefreshButton } from './views/Readout';

export function LocalManagementSection({ api }: { readonly api: LocalManagementApi }) {
  const { state, refresh } = useLocalManagement(api);
  return <Card as="section" padding="lg" className="settings-card" aria-label="本机状态">
    <div className="settings-card__header"><h2>本机状态</h2>
      <RefreshButton label="本机状态" disabled={state.kind === 'loading'} onClick={() => void refresh()} />
    </div>
    <div className="settings-live" aria-live="polite" aria-busy={state.kind === 'loading'}>
      {state.kind === 'ready' ? <Readout rows={[
        ['初始化', '已完成'], ['本机服务', state.status.phase === 'reconciled' ? '运行中 · 恢复完成' : '运行中'],
        ['版本', state.status.version],
      ]} /> : state.kind === 'loading' ? <p role="status">正在读取…</p>
        : <Notice tone="warning" title={state.kind === 'unknown' ? '状态未确认' : '状态读取失败'}>{'message' in state ? state.message : null}</Notice>}
    </div>
  </Card>;
}
