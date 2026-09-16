import React, { useEffect, useRef, useState } from 'react';

const date = (value) => value ? new Date(value).toLocaleString('zh-CN') : '暂无记录';
async function read(url, signal) {
  const response = await fetch(url, { signal, cache: 'no-store' });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || '无法读取本机看板');
  return value;
}

export default function KanbanView({ dshUrl }) {
  const [board, setBoard] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const active = useRef(null);
  async function refresh() {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError('');
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(25000)]);
    try {
      const state = await read('/api/kanban/status', signal);
      if (controller.signal.aborted) return;
      setStatus(state);
      if (state.state !== 'configured') throw new Error(state.message);
      const value = await read('/api/kanban/board', signal);
      if (!/^\/api\/kanban\/frame\/[a-f0-9]{64}$/.test(value.frameUrl || '') || typeof value.requiresScripts !== 'boolean') throw new Error('看板响应不兼容，请更新 GUI 和 Kanban。');
      if (!controller.signal.aborted) { setBoard(value); setStatus(value.status || state); }
    } catch (e) {
      if (!controller.signal.aborted) setError(e.name === 'TimeoutError' ? '读取看板超时，请重试。' : e.message);
    } finally {
      if (active.current === controller) { active.current = null; setBusy(false); }
    }
  }
  useEffect(() => { refresh(); return () => { active.current?.abort(); active.current = null; }; }, []);
  return <section className="kanban-view" aria-label="本机任务看板">
    <div className="kanban-toolbar">
      <div>
        <strong>{board ? `${board.taskCount} 张卡片` : '本机 Kanban'}</strong>
        <p>最近同步记录：{date(status?.lastSyncAt)}{status?.version ? ` · Kanban ${status.version}` : ''}</p>
        <p>刷新读取本机数据。共享板的新变更需要先在工作台让 Agent 同步。</p>
      </div>
      <div className="kanban-actions">
        {dshUrl && <a className="primary-link" href={dshUrl} target="_blank" rel="noopener noreferrer">前往工作台操作</a>}
        <button type="button" onClick={refresh} disabled={busy}>{busy ? '读取中…' : '刷新本机看板'}</button>
      </div>
    </div>
    {error && <p className="kanban-error" role="alert">{board ? '当前展示上次快照 · ' : ''}{error}</p>}
    {!dshUrl && <p className="kanban-hint">需要修改或同步时，请打开 DSH 工作台并交给 Agent 处理。</p>}
    {board && <>
      <p className="kanban-hint">本机快照：{date(board.updatedAt)}</p>
      <iframe title="Kanban 看板" className="kanban-frame" sandbox={board.requiresScripts ? 'allow-scripts' : ''} src={board.frameUrl} />
    </>}
    {!board && !busy && !error && <p>暂无看板快照。</p>}
  </section>;
}
