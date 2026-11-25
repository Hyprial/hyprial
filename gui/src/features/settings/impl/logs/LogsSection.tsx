import { useCallback, useEffect, useRef, useState } from 'react';
import { ClientError } from '../../../../client';
import type { LogPageResult } from '../views/contracts';
import { Button, Card, InfoTip, Notice } from '../../../../components/primitives';
import type { LogsApi } from './api';

import { RefreshButton, readoutTime } from '../views/Readout';

const events = { 'daemon.started': '服务启动', 'daemon.stopped': '服务停止', 'worker.exited': 'Worker 退出', 'dead-letter': '死信', other: '其他事件（详情已隐藏）' };
const levels = { debug: '调试', info: '信息', warn: '提醒', error: '错误' };
const components = { daemon: '服务', adapter: '平台接入', worker: 'Worker', other: '其他组件' };
const reasons = { 'logs-unavailable': '日志目录不可用', 'discovery-limit': '文件发现达到上限', 'unsafe-path': '已排除不安全路径', 'unreadable-file': '部分文件无法读取', 'invalid-record': '存在损坏或超长记录', 'scan-limit': '单页扫描达到上限' };
type State = { kind: 'idle' | 'loading' | 'failed' | 'unknown' | 'expired' } | { kind: 'ready'; page: LogPageResult; cursors: readonly (string | undefined)[] };

export function LogsSection({ api }: { readonly api: LogsApi }) {
  const [snapshot, setSnapshot] = useState<{ api: LogsApi; state: State }>({ api, state: { kind: 'idle' } });
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const state: State = snapshot.api === api ? snapshot.state : { kind: 'idle' };
  const query = useCallback(async (cursors: readonly (string | undefined)[] = [undefined]) => {
    const current = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSnapshot({ api, state: { kind: 'loading' } });
    try {
      const page = await api.read(cursors.at(-1), controller.signal);
      if (current === generation.current && !controller.signal.aborted) setSnapshot({ api, state: { kind: 'ready', page, cursors } });
    } catch (error) {
      if (current === generation.current && !controller.signal.aborted) {
        setSnapshot({ api, state: { kind: error instanceof ClientError && error.code === 'LOG_CURSOR_INVALID' ? 'expired'
          : error instanceof ClientError && error.dispatch === 'unknown' ? 'unknown' : 'failed' } });
      }
    }
  }, [api]);
  useEffect(() => {
    void query();
    return () => { generation.current++; pending.current?.abort(); };
  }, [query]);
  return <Card as="section" padding="lg" className="settings-card" aria-label="日志事件摘要">
    <div className="settings-card__header"><h2>日志事件摘要</h2>
      <RefreshButton label="日志事件摘要" disabled={state.kind === 'loading'} onClick={() => void query()} />
    </div>
    <div className="settings-live" aria-live="polite" aria-busy={state.kind === 'loading'}>
      {state.kind === 'idle' && <p>尚未查询日志摘要。</p>}
      {state.kind === 'loading' && <p role="status">正在读取日志摘要…</p>}
      {state.kind === 'expired' && <Notice tone="warning" title="日志快照已失效">日志可能已轮转或快照已过期，请重新查询。</Notice>}
      {(state.kind === 'failed' || state.kind === 'unknown') && <Notice tone="warning" title={state.kind === 'unknown' ? '日志查询结果未知' : '日志查询失败'}>未取得本页摘要，请手动重新查询。</Notice>}
      {state.kind === 'ready' && <>
        {state.page.partial && <Notice tone="warning" title="日志摘要不完整">{state.page.partialReasons.map(reason => reasons[reason]).join('；')}。本页跳过 {state.page.skippedLines} 条记录。</Notice>}
        {state.page.entries.length === 0 ? <p>本页没有可显示的安全事件摘要。</p> : <div className="settings-table-scroll settings-log-scroll" role="region" aria-label="日志记录" tabIndex={0}><table className="settings-table">
          <thead><tr><th scope="col">时间</th><th scope="col">级别</th><th scope="col">组件</th><th scope="col">事件</th></tr></thead>
          <tbody>{state.page.entries.map((row, index) => <tr key={index}>
            <td><time dateTime={row.timestamp}>{readoutTime(row.timestamp)}</time></td><td>{levels[row.level]}</td>
            <td>{components[row.component]}</td><td>{events[row.event]}</td>
          </tr>)}</tbody>
        </table></div>}
      </>}
    </div>
    <div className="settings-actions">
      <InfoTip label="日志摘要说明" content="仅含安全事件摘要，不含消息正文；按文件分页，非全局时间顺序。" />
      {state.kind === 'ready' && (state.cursors.length > 1 || state.page.nextCursor) && <>
        <Button variant="text" disabled={state.cursors.length === 1}
          onClick={() => void query(state.cursors.slice(0, -1))}>上一页</Button>
        <span>第 {state.cursors.length} 页</span>
        <Button variant="text" disabled={!state.page.nextCursor}
          onClick={() => { if (state.page.nextCursor) void query([...state.cursors, state.page.nextCursor]); }}>下一页</Button>
      </>}
    </div>
  </Card>;
}
