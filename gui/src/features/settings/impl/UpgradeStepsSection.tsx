import { useCallback, useEffect, useRef, useState } from 'react';
import { ClientError } from '../../../client';
import { InfoTip, Notice } from '../../../components/primitives';
import type { UpgradeStatusApi, UpgradeStepsView } from './upgradeApi';

import { Readout, RefreshButton, readoutTime } from './views/Readout';

const labels = { 'mcp-entry-v1': '旧启动参数迁移', 'autoupdate-timer-v1': '旧自动更新定时器退役', other: '其他迁移步骤' };
const states = { pending: '待处理', done: '记录为完成', failed: '记录为失败', unknown: '状态未知' };
type State = { kind: 'idle' | 'loading' | 'failed' | 'unknown' } | { kind: 'ready'; value: UpgradeStepsView };
export function UpgradeStepsSection({ api }: { readonly api: UpgradeStatusApi }) {
  const [snapshot, setSnapshot] = useState<{ api: UpgradeStatusApi; state: State }>({ api, state: { kind: 'idle' } });
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const state: State = snapshot.api === api ? snapshot.state : { kind: 'idle' };
  const refresh = useCallback(async () => {
    if (!api.steps) return;
    const scope = ++generation.current;
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    setSnapshot({ api, state: { kind: 'loading' } });
    try {
      const value = await api.steps(controller.signal);
      if (scope === generation.current && !controller.signal.aborted) setSnapshot({ api, state: { kind: 'ready', value } });
    } catch (error) {
      if (scope === generation.current && !controller.signal.aborted) setSnapshot({ api, state: {
        kind: error instanceof ClientError && error.dispatch === 'unknown' ? 'unknown' : 'failed',
      } });
    }
  }, [api]);
  useEffect(() => {
    void refresh();
    return () => { ++generation.current; pending.current?.abort(); };
  }, [refresh]);
  if (!api.steps) return null;
  return <section className="settings-subsection" aria-label="升级迁移记录">
    <div className="settings-card__header">
      <h3>升级迁移记录</h3>
      <RefreshButton label="升级迁移记录" disabled={state.kind === 'loading'} onClick={() => void refresh()} />
    </div>
    <div className="settings-live" aria-live="polite" aria-busy={state.kind === 'loading'}>
      {state.kind === 'idle' && <p>尚未查询迁移记录。</p>}
      {state.kind === 'loading' && <p role="status">正在读取迁移记录…</p>}
      {(state.kind === 'failed' || state.kind === 'unknown') && <Notice tone="warning" title={state.kind === 'unknown' ? '迁移记录查询结果未知' : '迁移记录读取失败'}>未取得可用记录，请手动查询。</Notice>}
      {state.kind === 'ready' && <>
        {state.value.steps.length === 0 ? <p>暂无迁移记录</p> : <div className="settings-table-scroll"><table className="settings-table">
          <thead><tr><th scope="col">迁移项</th><th scope="col">状态</th><th scope="col">尝试次数</th><th scope="col">记录时间</th></tr></thead>
          <tbody>{state.value.steps.map((step, index) => <tr key={index}>
            <th scope="row">{labels[step.id]}</th><td>{states[step.state]}</td><td>{step.attempts}</td><td>{readoutTime(step.updatedAt)}</td>
          </tr>)}</tbody>
        </table></div>}
        <Readout rows={[
          ['旧启动配置文件数', state.value.legacyFileCount], ['系统范围旧进程数', state.value.legacyProcessCount],
        ]} />
        <InfoTip label="迁移记录说明" content="迁移完成不表示已更新到最新版本；旧进程数是系统范围观测值。" />
      </>}
    </div>
  </section>;
}
