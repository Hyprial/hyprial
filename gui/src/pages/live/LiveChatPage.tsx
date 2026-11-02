import { useState, useSyncExternalStore } from 'react';
import { Button } from '../../components/primitives';
import { AgentChatSection } from '../../features/agent-chat';
import type { LiveRuntime } from './runtime';
import './live.css';
export type ChatPageRuntime = Pick<LiveRuntime, 'status' | 'error' | 'chat'> & { readonly sourceLabel?: string };
export function LiveChatPage({ live }: { readonly live: ChatPageRuntime }) {
  const state = useSyncExternalStore(live.chat.subscribe, live.chat.getSnapshot);
  const [infoExpanded, setInfoExpanded] = useState(false);
  const targetId = state.activeTargetId;
  const entry = targetId ? state.entries[targetId] : undefined;
  const ready = !entry?.operation?.confirmationPending && live.status === 'ready' && state.status === 'ready' && !!entry?.conversationId &&
    state.targets.some((target) => target.id === targetId && target.availability === 'available');
  return <div className="live-chat-page">
    <div className="live-status-row" role="status">
      {live.sourceLabel && <span>{live.sourceLabel}</span>}
      {live.status === 'error' || state.status === 'error' ? live.error ?? state.error :
        live.status === 'loading' || state.status === 'loading' ? import.meta.env.DEV && live.sourceLabel ? '正在加载隔离聊天场景…' : '正在连接真实聊天服务…' : import.meta.env.DEV && live.sourceLabel ? '以下为隔离契约演示数据。' : '聊天记录来自当前本机服务。'}
      {targetId && <><Button variant="ghost" onClick={() => { void live.chat.refresh(targetId); }}>刷新消息</Button>
        <Button variant="ghost" onClick={() => { void live.chat.reloadHistory(targetId); }}>重新加载历史</Button></>}
      {entry?.operation?.status === 'unknown' && targetId &&
        <Button onClick={() => { void live.chat.reconcile(targetId); }}>查询原消息结果</Button>}
      {entry?.operation?.confirmationPending && targetId && <><span>{entry.operation.error ?? '正在确认消息接收记录…'}</span>
        <Button onClick={() => { void live.chat.confirmAcceptance(targetId); }}>确认接收记录</Button></>}
      {entry?.operation?.status === 'definitive-failure' && targetId &&
        <Button onClick={() => live.chat.resetFailure(targetId)}>修改后重新发送</Button>}
    </div>
    {entry?.partial && <p role="status">正在继续加载历史消息。<Button variant="ghost" onClick={() => { void live.chat.refresh(); }}>继续加载消息</Button></p>}
    <AgentChatSection targets={state.targets} targetState={state.status} targetError={state.error} activeTargetId={targetId}
      messages={entry?.messages ?? []} draft={targetId ? state.drafts[targetId] ?? '' : ''}
      capability={ready ? 'ready' : live.status === 'error' ? 'unavailable' : 'unconfirmed'}
      operation={entry?.operation} successHint="消息已被服务接收，回复以聊天记录为准。"
      history={{ targetId: targetId ?? '', status: entry?.historyStatus ?? 'loading', error: entry?.historyError }}
      infoExpanded={infoExpanded} onInfoExpandedChange={setInfoExpanded}
      onSelectTarget={(id) => { void live.chat.select(id); }}
      onDraftChange={(id, value) => live.chat.setDraft(id, value)}
      onSend={(id, text) => { void live.chat.send(id, text); }}
      onRetry={(key) => { void live.chat.retry(key); }} />
  </div>;
}
