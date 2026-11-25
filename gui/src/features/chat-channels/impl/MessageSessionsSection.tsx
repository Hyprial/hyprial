/**
 * Decision record (read-only session directory, slice G1, 2026-11-18)
- The directory lists `conversations.list` heads and reads the selected one's
  conversation document: selection holds the head's `documentId`, never its
  send id, because `conversations.history` takes only the document id.
- The `recent` source (a directory built from `message.query` rows) is gone: no
  composition used it, and its rows carry only send ids, which name no
  document to read.
 */
import { Button } from '../../../components/primitives';
import { useEffect, useRef, useState } from 'react';
import { ClientError, methodRefusal } from '../../../client';
import { useNotifyUnavailable } from '../../../components/notifications';
import { useGuiCapabilities } from '../../../app/capabilities';
import { useAgentDirectory, directoryRowToView, type AgentDirectoryRow } from '../../agents';
import { agentUriDisplayName } from '../../agent-chat';
import { type ConversationPage, type HistoryPage, type MessageReadApi } from './message-read-api';

type ReadFailure = { readonly code: string; readonly requestId: string | null };
type Directory = { state: 'loading' | 'unavailable' } | { state: 'error'; failure: ReadFailure } | { state: 'ready'; page: ConversationPage; loadingMore: boolean };
type History = { state: 'loading'; conversationId: string | null } | { state: 'error'; conversationId: string; failure: ReadFailure } | { state: 'ready'; conversationId: string; page: HistoryPage; loadingMore: boolean; moreFailure?: ReadFailure };

function failureOf(error: unknown): ReadFailure {
  return error instanceof ClientError
    ? { code: error.code, requestId: error.diagnostics.requestId }
    : { code: 'READ_FAILED', requestId: null };
}

function FailureDetail({ failure }: { readonly failure: ReadFailure }) {
  return <details className="message-sessions__diagnostics">
    <summary>诊断详情</summary>
    <p>错误代码：<code>{failure.code}</code></p>
    {failure.requestId !== null && <p>请求 ID：<code>{failure.requestId}</code></p>}
  </details>;
}

export function MessageSessionsSection({ api }: { readonly api?: MessageReadApi }) {
  const notifyUnavailable = useNotifyUnavailable();
  const currentApi = useRef(api);
  const apiEpoch = useRef(0);
  if (currentApi.current !== api) apiEpoch.current += 1;
  currentApi.current = api;
  const directoryGeneration = useRef(0);
  const historyGeneration = useRef(0);
  const [directory, setDirectory] = useState<Directory>({ state: 'loading' });
  const [selection, setSelection] = useState<{ id: string; epoch: number } | null>(null);
  const [selectedActor, setSelectedActor] = useState<{ id: string; epoch: number } | null>(null);
  const selected = selection?.epoch === apiEpoch.current ? selection.id : null;
  const activeActor = selectedActor?.epoch === apiEpoch.current ? selectedActor.id : null;
  const [history, setHistory] = useState<History>({ state: 'loading', conversationId: null });
  const [historyEpoch, setHistoryEpoch] = useState(-1);

  useEffect(() => {
    const generation = ++directoryGeneration.current;
    if (!api) { setSelection(null); setHistory({ state: 'loading', conversationId: null }); return; }
    let active = true;
    setDirectory({ state: 'loading' });
    void api.conversations()
      .then(page => { if (active && currentApi.current === api && directoryGeneration.current === generation) setDirectory({ state: 'ready', page, loadingMore: false }); })
      .catch(error => { if (active && currentApi.current === api && directoryGeneration.current === generation) {
        setDirectory(methodRefusal(error) !== null ? { state: 'unavailable' } : { state: 'error', failure: failureOf(error) });
      } });
    return () => { active = false; };
  }, [api]);

  useEffect(() => {
    const generation = ++historyGeneration.current;
    if (!api || !selected) return;
    let active = true;
    setHistoryEpoch(apiEpoch.current);
    setHistory({ state: 'loading', conversationId: selected });
    void api.history(selected).then(page => { if (active && currentApi.current === api && historyGeneration.current === generation) setHistory({ state: 'ready', conversationId: selected, page, loadingMore: false }); })
      .catch(error => { if (active && currentApi.current === api && historyGeneration.current === generation) setHistory({ state: 'error', conversationId: selected, failure: failureOf(error) }); });
    return () => { active = false; };
  }, [api, selected]);

  const conversations = directory.state === 'ready' ? directory.page.entries : [];

  async function moreConversations() {
    if (!api || directory.state !== 'ready' || directory.loadingMore || !directory.page.nextCursor) return;
    const current = directory.page;
    const cursor = current.nextCursor;
    if (!cursor) return;
    const generation = ++directoryGeneration.current;
    setDirectory({ state: 'ready', page: current, loadingMore: true });
    try {
      const page = await api.conversations(cursor);
      if (currentApi.current === api && directoryGeneration.current === generation)
        setDirectory({ state: 'ready', page: { entries: [...current.entries, ...page.entries], nextCursor: page.nextCursor }, loadingMore: false });
    } catch (error) {
      if (currentApi.current === api && directoryGeneration.current === generation) {
        const refusal = methodRefusal(error);
        if (refusal !== null) {
          setDirectory({ state: 'ready', page: current, loadingMore: false });
          notifyUnavailable('读取消息', refusal);
        } else setDirectory({ state: 'error', failure: failureOf(error) });
      }
    }
  }

  async function more(cursor: string) {
    if (!api || !selected || history.state !== 'ready' || history.loadingMore) return;
    const conversationId = selected;
    const current = history.page;
    const generation = ++historyGeneration.current;
    setHistory({ ...history, loadingMore: true, moreFailure: undefined });
    try {
      const page = await api.history(conversationId, cursor);
      if (page.nextCursor === cursor) throw new Error('历史分页游标重复');
      if (currentApi.current === api && historyGeneration.current === generation) setHistory(value => value.conversationId === conversationId ?
        { state: 'ready', conversationId, page: { entries: [...current.entries, ...page.entries], nextCursor: page.nextCursor }, loadingMore: false } : value);
    } catch (error) {
      if (currentApi.current !== api || historyGeneration.current !== generation) return;
      const refusal = methodRefusal(error);
      if (refusal !== null) {
        setHistory(value => value.conversationId === conversationId ? { ...history, loadingMore: false } : value);
        notifyUnavailable('读取消息', refusal);
        return;
      }
      setHistory(value => value.conversationId === conversationId ?
        { ...history, loadingMore: false, moreFailure: failureOf(error) } : value);
    }
  }

  const agents = useAgentDirectory(useGuiCapabilities().agents);
  const agentRows = agents.status === 'ready' ? agents.rows : [];
  const conversationForAgent = (row: AgentDirectoryRow) => conversations.find(conversation => conversation.peer === row.uri) ?? null;
  const selectedAgent = agentRows.find(row => row.actor === activeActor)
    ?? agentRows.find(row => conversationForAgent(row)?.documentId === selected)
    ?? null;
  const roleSummary = (row: AgentDirectoryRow) => directoryRowToView(row).role || '职责未确认';
  const availability = (row: AgentDirectoryRow) => row.runState.runtime === 'online'
    ? '可用性：可用' : row.runState.runtime === 'offline' ? '可用性：不可用' : '可用性未确认';

  const selectAgent = (row: AgentDirectoryRow) => {
    const conversation = conversationForAgent(row);
    setSelectedActor({ id: row.actor, epoch: apiEpoch.current });
    setSelection(conversation ? { id: conversation.documentId, epoch: apiEpoch.current } : null);
  };

  const listPlaceholder = (() => {
    if (agents.status === 'loading') return <p className="chat-list-note" role="status">正在读取 Agent 目录</p>;
    if (agents.status === 'error') return null;
    if (agents.status === 'unwired') return null;
    if (agents.status === 'ready') return <p className="chat-list-note" role="status">还没有 Agent。</p>;
    return <p className="chat-list-note" role="status">Agent 目录尚未确认</p>;
  })();
  const noConversationsYet = directory.state === 'ready' && conversations.length === 0
    ? <p className="chat-list-note" role="status">暂无历史会话</p> : null;

  const agentDirectoryError = agents.status === 'error' ? <div role="alert">
    <p>Agent 目录读取失败</p>
    <details className="message-sessions__diagnostics">
      <summary>诊断详情</summary>
      <p>{agents.error ?? '未提供错误信息'}</p>
    </details>
  </div> : null;

  const directoryError = !api ? <p role="status">历史会话目录尚未确认</p>
    : directory.state === 'loading' ? <p role="status">正在读取历史会话</p>
      : directory.state === 'error' ? <div role="alert"><p>历史会话目录读取失败</p><FailureDetail failure={directory.failure} /></div>
        : null;

  const threadMessages = (() => {
    if (!selectedAgent && !selected) return <p role="status">选择一个 Agent 开始对话。</p>;
    if (selectedAgent && !conversationForAgent(selectedAgent)) return <p role="status">此 Agent 暂无消息。</p>;
    if (!selected) return <p role="status">此 Agent 暂无消息。</p>;
    if (historyEpoch !== apiEpoch.current || history.conversationId !== selected || history.state === 'loading') return <p role="status">正在读取历史消息</p>;
    if (history.state === 'error') return <div role="alert"><p>消息暂时无法读取。请稍后再试。</p><FailureDetail failure={history.failure} /></div>;
    if (history.page.entries.length === 0) return <p role="status">此会话暂无消息</p>;
    return <ol>{history.page.entries.map(entry =>
      <li key={entry.messageId} className="chat-message"><strong>{agentUriDisplayName(agentRows, entry.from)}</strong>：{entry.message}</li>)}</ol>;
  })();

  const threadFooter = selected && historyEpoch === apiEpoch.current && history.conversationId === selected && history.state === 'ready' && history.moreFailure
    ? <div role="alert"><p>更多历史消息读取失败</p><FailureDetail failure={history.moreFailure} /></div>
    : null;
  const loadMore = selected && historyEpoch === apiEpoch.current && history.conversationId === selected && history.state === 'ready' && history.page.nextCursor
    ? <Button variant="text" size="compact" disabled={history.loadingMore} onClick={() => void more(history.page.nextCursor!)}>加载更多</Button>
    : null;

  return <section aria-label="历史会话目录" className="message-sessions">
    <div className="chat-layout chat-layout--readonly" data-info="collapsed">
      <aside className="chat-list" aria-label="Agent 列表">
        <header className="chat-list-head">Agent<span className="chat-list-count">{agentRows.length}</span></header>
        <div className="chat-list-scroll">
          {agentDirectoryError}
          {directoryError}
          {agentRows.length > 0 ? <ul>{agentRows.map(row => {
            const conversation = conversationForAgent(row);
            const active = activeActor === row.actor || selected === conversation?.documentId;
            return <li key={row.actor}>
              <button type="button" className={`chat-list-item${active ? ' chat-list-item--selected' : ''}`}
                aria-label={row.displayName} aria-pressed={active} onClick={() => selectAgent(row)}>
                <span className="chat-list-avatar" aria-hidden="true">{row.displayName.slice(0, 1)}</span>
                <span className="chat-list-copy"><strong>{row.displayName}</strong><span>{roleSummary(row)}</span></span>
              </button>
            </li>;
          })}</ul> : listPlaceholder}
          {noConversationsYet}
          {directory.state === 'ready' && directory.page.nextCursor &&
            <Button variant="text" size="compact" disabled={directory.loadingMore} onClick={() => void moreConversations()}>加载更多会话</Button>}
        </div>
      </aside>

      <section className="chat-thread" aria-label="只读消息">
        <header className="chat-head">
          <h2>{selectedAgent?.displayName ?? (selected ? '只读会话' : '选择 Agent')}</h2>
        </header>
        <div className="chat-messages">
          {threadMessages}
          {threadFooter}
          {loadMore}
        </div>
      </section>

      <aside className="chat-info" aria-label="Agent 信息">
        {selectedAgent ? <>
          <h2>{selectedAgent.displayName}</h2>
          <p>{roleSummary(selectedAgent)}</p>
          <p>{availability(selectedAgent)}</p>
          <p>{(() => { const label = directoryRowToView(selectedAgent).modelLabel; return !label || label === '未指定模型' ? '模型未确认' : label; })()}</p>
        </> : <p>选择一个 Agent 查看信息。</p>}
      </aside>
    </div>
  </section>;
}
