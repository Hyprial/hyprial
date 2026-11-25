/** Explicit route reads and writes; unknown mutations require a fresh read, never replay. */
import { useCallback, useEffect, useRef, useState } from 'react';
import { methodRefusal } from '../../../../client';
import { useNotifyUnavailable } from '../../../../components/notifications';
import { Button, Dialog, Field, Select } from '../../../../components/primitives';
import {
  routeRefusal, routeWriteFailure, type AdapterRoutesApi, type RouteAction, type RouteMutation, type RouteSnapshot,
} from './api';

function shortId(id: string): string { return id.length > 24 ? `${id.slice(0, 10)}…${id.slice(-6)}` : id; }
function chatLabel(row: { chatId: string; chatType: string | null; chatName?: string | null }): string {
  const kind = row.chatType === 'group' ? '群聊' : row.chatType === 'p2p' ? '私聊' : '聊天';
  return [row.chatName?.trim(), kind, shortId(row.chatId)].filter(Boolean).join(' · ');
}

export function RouteSettings({ name, api }: { readonly name?: string; readonly api: AdapterRoutesApi }) {
  const source = useRef(0);
  const notifyUnavailable = useNotifyUnavailable();
  const notifyRef = useRef(notifyUnavailable);
  notifyRef.current = notifyUnavailable;
  function refused(action: RouteAction, subject: string): boolean {
    const refusal = routeRefusal(api, action, name ?? '');
    if (!refusal) return false;
    notifyRef.current(subject, refusal);
    return true;
  }
  const [snapshot, setSnapshot] = useState<RouteSnapshot | null>(null);
  const [candidates, setCandidates] = useState<Awaited<ReturnType<AdapterRoutesApi['candidates']>>>([]);
  const [adding, setAdding] = useState(false);
  const [readingCandidates, setReadingCandidates] = useState(false);
  const [routeName, setRouteName] = useState('');
  const [chatId, setChatId] = useState('');
  const [makeDefault, setMakeDefault] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [clearDefault, setClearDefault] = useState(false);

  const load = useCallback(async (manual = false) => {
    if (!name) return;
    const refusal = routeRefusal(api, 'list', name);
    if (refusal) {
      if (manual) notifyRef.current('读取路由', refusal);
      else { setSnapshot(null); setBusy(false); }
      return;
    }
    const current = source.current;
    setBusy(true);
    try {
      const result = await api.list(name);
      if (source.current !== current) return;
      setSnapshot(result); setUnconfirmed(false); setMessage(null);
    } catch (error) {
      if (source.current === current) {
        const thrown = methodRefusal(error);
        if (thrown) {
          if (manual) notifyRef.current('读取路由', thrown);
          else setSnapshot(null);
        } else { setSnapshot(null); setMessage('路由读取失败。'); }
      }
    } finally { if (source.current === current) setBusy(false); }
  }, [api, name]);

  useEffect(() => {
    source.current += 1;
    setSnapshot(null); setCandidates([]); setChatId(''); setRouteName('');
    setAdding(false); setReadingCandidates(false);
    setUnconfirmed(false); setNeedsReload(false); setMessage(null); setRemoving(null); setMakeDefault(false);
    void load();
    return () => { source.current += 1; };
  }, [load]);

  async function readCandidates(): Promise<void> {
    if (!name || busy || refused('candidates', '读取候选')) return;
    const current = source.current;
    setBusy(true); setReadingCandidates(true); setMessage(null);
    try {
      const rows = await api.candidates(name);
      if (current !== source.current) return;
      setCandidates(rows); setChatId('');
      setMessage(rows.length ? `已读取 ${rows.length} 个聊天。` : '暂无候选，请先在平台向 Agent 发送消息。');
    } catch (error) {
      if (current === source.current) {
        const thrown = methodRefusal(error);
        if (thrown) notifyRef.current('读取候选', thrown);
        else { setCandidates([]); setChatId(''); setMessage('候选读取失败。'); }
      }
    }
    finally { if (current === source.current) { setBusy(false); setReadingCandidates(false); } }
  }

  async function mutate(subject: string, action: () => Promise<RouteMutation>, failure: string): Promise<void> {
    if (busy || unconfirmed || !snapshot) return;
    const current = source.current;
    setBusy(true); setMessage(null);
    try {
      const result = await action();
      if (current !== source.current) return;
      setSnapshot({ ...result.adapter, routes: result.adapter.routes.map(row => ({ ...row,
        chatName: row.chatName ?? candidates.find(candidate => candidate.chatId === row.nativeId)?.chatName,
      })) }); setNeedsReload(!result.reloaded);
      setRemoving(null); setRouteName(''); setChatId('');
      setAdding(false);
      setMessage(result.reloaded ? null : '路由已保存，请重新加载配置。');
    } catch (error) {
      if (current === source.current) {
        const thrown = methodRefusal(error);
        if (thrown) { notifyRef.current(subject, thrown); setRemoving(null); }
        else {
          const result = routeWriteFailure(error, failure);
          setUnconfirmed(result.blocked); setRemoving(null); setMessage(result.message);
        }
      }
    }
    finally { if (current === source.current) setBusy(false); }
  }

  async function reloadConfiguration(): Promise<void> {
    if (busy || refused('reload', '重新加载配置')) return;
    const current = source.current;
    setBusy(true);
    try {
      await api.reload();
      if (current !== source.current) return;
      setNeedsReload(false);
      await load();
    } catch (error) {
      if (current === source.current) {
        const thrown = methodRefusal(error);
        if (thrown) notifyRef.current('重新加载配置', thrown);
        else setMessage('配置加载结果未确认，请刷新路由。');
      }
    }
    finally { if (current === source.current) setBusy(false); }
  }

  return <section className="card agent-detail-card" aria-labelledby="agent-routes-title">
    <div className="agent-section-heading">
      <h3 id="agent-routes-title" className="agent-detail-card-title">消息路由</h3>
      {name && <Button variant="secondary" size="compact" disabled={busy} onClick={() => void load(true)}>刷新路由</Button>}
    </div>
    {!name ? <p>先绑定平台接入。</p>
      : <>
        {snapshot && <>
          {snapshot.routes.length === 0 ? <p>暂无消息路由</p> : <div className="agent-table-scroll"><table className="agent-detail-table">
            <thead><tr><th>路由</th><th>目标</th><th>操作</th></tr></thead>
            <tbody>{snapshot.routes.map(row => <tr key={row.name}>
              <td>{row.name}{snapshot.defaultRoute === row.name && <span> · 默认</span>}</td>
              <td title={row.nativeId}>{row.chatName || row.nickname || (row.nativeId ? shortId(row.nativeId) : row.members?.join('、'))}
                {row.nativeId && (row.chatName || row.nickname) && <div className="agent-detail-card-hint">{shortId(row.nativeId)}</div>}
              </td>
              <td><Button variant="text" size="compact" aria-label={`删除 ${row.name}`} disabled={busy || unconfirmed}
                onClick={() => { if (refused('remove', '删除路由')) return; setRemoving(row.name); setClearDefault(false); }}>删除</Button></td>
            </tr>)}</tbody>
          </table></div>}
          {snapshot.namesStatus && snapshot.namesStatus !== 'complete' && <p role="status">部分聊天名称未能读取，显示聊天编号。</p>}
          {!adding && <Button variant="secondary" disabled={busy || unconfirmed}
            onClick={() => { if (!refused('add', '添加路由')) setAdding(true); }}>添加路由</Button>}
          {adding && <form className="agent-form" onSubmit={event => {
            event.preventDefault();
            if (routeName.trim() && chatId && !refused('add', '保存路由')) void mutate('保存路由', () => api.add(name, routeName.trim(), chatId, makeDefault, snapshot.revision), '保存结果未确认，请刷新路由。');
          }}>
            <Button variant="secondary" type="button" disabled={busy} onClick={() => void readCandidates()}>{readingCandidates ? '正在读取聊天…' : '读取候选'}</Button>
            <Field label="路由名称" htmlFor="agent-route-name"><input id="agent-route-name" value={routeName} disabled={busy || unconfirmed} onChange={event => setRouteName(event.target.value)} /></Field>
            <Field label="目标聊天" htmlFor="agent-route-chat"><Select id="agent-route-chat" value={chatId} disabled={busy || unconfirmed}
              onValueChange={setChatId} options={[{ value: '', label: '选择聊天' }, ...candidates.map(row => ({ value: row.chatId, label: chatLabel(row) }))]} /></Field>
            {chatId && <p className="agent-detail-card-hint">{chatId}</p>}
            <label><input type="checkbox" checked={makeDefault} disabled={busy || unconfirmed} onChange={event => setMakeDefault(event.target.checked)} />设为默认路由</label>
            <Button type="submit" disabled={busy || unconfirmed || !routeName.trim() || !chatId}>保存路由</Button>
            <Button type="button" variant="text" disabled={busy} onClick={() => setAdding(false)}>取消</Button>
          </form>}
        </>}
        {needsReload && <Button variant="secondary" disabled={busy} onClick={() => void reloadConfiguration()}>重新加载全部配置</Button>}
      </>}
    {message && <p role="status">{message}</p>}
    <Dialog open={removing !== null} title="删除路由" onClose={() => { if (!busy) setRemoving(null); }}
      actions={[{ label: '取消', disabled: busy, onClick: () => setRemoving(null), variant: 'secondary' },
        { label: '确认删除', disabled: busy || unconfirmed || (snapshot?.defaultRoute === removing && !clearDefault),
          onClick: () => { if (name && snapshot && removing) void mutate('删除路由', () => api.remove(name, removing, clearDefault, snapshot.revision), '删除结果未确认，请刷新路由。'); } }]}>
      <p>{removing}</p>
      {snapshot?.defaultRoute === removing && <label><input type="checkbox" checked={clearDefault} disabled={busy} onChange={event => setClearDefault(event.target.checked)} />同时清除默认路由</label>}
    </Dialog>
  </section>;
}
