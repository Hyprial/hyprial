import { Card, Notice } from '../../../components/primitives';
import type { UpgradeStatusApi } from './upgradeApi';
import { UpgradeStepsSection } from './UpgradeStepsSection';
import { useUpgradeStatus } from './useUpgradeStatus';
import { Readout, RefreshButton, readoutTime } from './views/Readout';

export function UpgradeStatusSection({ api }: { readonly api: UpgradeStatusApi }) {
  const { state, refresh } = useUpgradeStatus(api);
  const value = state.kind === 'ready' ? state.value : null;
  const scheduler = value ? value.active ? '执行中' : value.pending ? '等待执行' : value.running ? '空闲' : '未运行' : '未确认';
  return <Card as="section" padding="lg" className="settings-card" aria-label="服务更新状态">
    <div className="settings-card__header"><h2>服务更新状态</h2>
      <RefreshButton label="服务更新状态" disabled={state.kind === 'loading'} onClick={() => void refresh()} />
    </div>
    <div className="settings-live" aria-live="polite" aria-busy={state.kind === 'loading'}>
      {value && <Readout rows={[
        ['升级调度', scheduler],
        ['自动升级', value.autoUpgradeEnabled === null ? '未确认' : value.autoUpgradeEnabled ? '开启' : '关闭'],
        ['每日计划', value.schedule.map(slot => `${String(slot.hour).padStart(2, '0')}:${String(slot.minute).padStart(2, '0')}`).join('、') || '—'],
        ['下次计划', readoutTime(value.nextRunAt)],
        ['上次触发', readoutTime(value.lastTriggeredAt)],
        ['上次退出码', value.lastExitCode],
        ...(value.lastSkipReason ? [['上次调度', '已跳过'] as const] : []),
        ...(value.lastRun ? [['上次执行', value.lastRun.ok === true ? '成功' : value.lastRun.ok === false ? '失败' : '未确认'] as const] : []),
      ]} />}
      {value?.lastError && <Notice tone="warning" title="调度异常">请刷新核对，尚不能确认升级完成。</Notice>}
      {state.kind === 'loading' && <p role="status">正在读取…</p>}
      {state.kind === 'not-ready' && <Notice tone="warning" title="升级状态暂未就绪">请稍后刷新。</Notice>}
      {state.kind === 'failed' && <Notice tone="warning" title="升级状态查询失败">请检查本机服务后刷新。</Notice>}
      {state.kind === 'unknown' && <Notice tone="warning" title="查询结果未知">请刷新核对。</Notice>}
    </div>
    <UpgradeStepsSection api={api} />
  </Card>;
}
