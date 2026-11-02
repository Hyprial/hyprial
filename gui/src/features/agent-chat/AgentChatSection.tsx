import { useId, useLayoutEffect, useRef, type ReactNode } from 'react';
import { AgentAvatar } from '../../components/AgentAvatar';
import { AvailabilityBadge } from '../../components/Badge';
import { Button, EmptyState } from '../../components/primitives';
import type { SessionTarget, ConversationMessage } from '../../state/gui-view-model';
import { ChatComposer, type ChatComposerProps } from './ChatComposer';
import { AgentTargetField } from './AgentTargetField';
import { AgentHistoryField } from './AgentHistoryField';
import './chat.css';

export interface ChatWorkspaceViewProps {
  readonly successHint?: string;
  readonly notice?: string;
  readonly channelControl?: ReactNode;
  readonly renderAfterMessage?: (message: ConversationMessage) => ReactNode;
  readonly targetDetail?: string;
  readonly targets: readonly SessionTarget[];
  readonly targetState?: 'loading' | 'ready' | 'error';
  readonly targetError?: string;
  readonly activeTargetId: string | null;
  readonly messages: readonly ConversationMessage[];
  readonly draft: string;
  readonly capability?: ChatComposerProps['capability'];
  readonly operation?: ChatComposerProps['operation'];
  readonly history: {
    readonly targetId: string;
    readonly status: 'loading' | 'ready' | 'error';
    readonly error?: string;
    readonly stale?: boolean;
  };
  readonly infoExpanded: boolean;
  readonly onSelectTarget: (targetId: string) => void;
  readonly onDraftChange: ChatComposerProps['onDraftChange'];
  readonly onSend: ChatComposerProps['onSend'];
  readonly onRetry: ChatComposerProps['onRetry'];
  readonly onInfoExpandedChange: (expanded: boolean) => void;
  readonly onManageTarget?: (targetId: string) => void;
}
/** Controlled presentation. Only scroll positions and focus are remembered here. */
export function AgentChatSection({
  targets, targetState = 'ready', targetError, activeTargetId, messages, draft, capability = 'unavailable', operation,
  history, infoExpanded, onSelectTarget, onDraftChange, onSend, onRetry,
  onInfoExpandedChange, onManageTarget, successHint, notice, channelControl, renderAfterMessage, targetDetail,
}: ChatWorkspaceViewProps) {
  const active = targets.find((target) => target.id === activeTargetId);
  const currentId = active?.id ?? null;
  const thread = active ? messages.filter((message) => message.targetId === active.id) : [];
  const currentHistory = active && history.targetId === active.id ? history : undefined;
  const instanceId = useId();
  const titleId = `${instanceId}-title`;
  const infoId = `${instanceId}-info`;
  const messagesRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const previousTargetRef = useRef<string | null>(null);
  const previousExpandedRef = useRef(infoExpanded);
  const followRef = useRef(true);
  const positionsRef = useRef(new Map<string, { top: number; follow: boolean }>());
  const pendingTopRef = useRef<number | null>(null);

  useLayoutEffect(() => {
    const node = messagesRef.current;
    if (previousTargetRef.current !== currentId) {
      previousTargetRef.current = currentId;
      const saved = currentId === null ? undefined : positionsRef.current.get(currentId);
      followRef.current = saved?.follow ?? true;
      pendingTopRef.current = saved && !saved.follow ? saved.top : null;
    }
    if (node && pendingTopRef.current !== null) {
      // Empty or partial history can clamp scrollTop; retry when the offset fits.
      const maxTop = Math.max(0, node.scrollHeight - node.clientHeight);
      if (maxTop >= pendingTopRef.current || (currentHistory && currentHistory.status !== 'loading')) {
        node.scrollTop = Math.min(pendingTopRef.current, maxTop);
        pendingTopRef.current = null;
        rememberScroll();
      }
    } else if (node && followRef.current) {
      node.scrollTop = node.scrollHeight;
    }
  }, [currentId, messages, history]);

  useLayoutEffect(() => {
    if (previousExpandedRef.current && !infoExpanded) toggleRef.current?.focus();
    previousExpandedRef.current = infoExpanded;
  }, [infoExpanded]);

  function rememberScroll() {
    const node = messagesRef.current;
    if (!node || currentId === null || pendingTopRef.current !== null) return;
    const follow = node.scrollHeight - node.scrollTop - node.clientHeight <= 24;
    followRef.current = follow;
    positionsRef.current.set(currentId, { top: node.scrollTop, follow });
  }

  function cancelPendingRestore() {
    if (pendingTopRef.current === null) return;
    pendingTopRef.current = null;
    rememberScroll();
  }

  return (
    <section className="chat-workspace" data-testid="chat-layout" aria-label="聊天工作区" data-info={active && infoExpanded ? 'expanded' : 'collapsed'}>
      <AgentTargetField targets={targets} activeTargetId={currentId} onValueChange={onSelectTarget} state={targetState} error={targetError} />

      {!active ? <div className="chat-workspace__unselected"><EmptyState title="请选择聊天对象" /></div> : (
        <>
          <section className="chat-workspace__conversation" aria-labelledby={titleId}>
            <header className="chat-workspace__heading">
              <div><h2 id={titleId}>{active.displayName}</h2><p>{active.role}</p></div>
              <button ref={toggleRef} data-testid="chat-info-toggle" type="button" className="chat-workspace__info-toggle"
                aria-expanded={infoExpanded} aria-controls={infoId}
                onClick={() => onInfoExpandedChange(!infoExpanded)}>信息</button>
            </header>
            {notice && <p role="note" className="chat-unavailable">{notice}</p>}
            {channelControl}
            <AgentHistoryField target={active} messages={thread} history={currentHistory} busy={operation?.status === 'in-flight'}
              renderAfterMessage={renderAfterMessage} scrollRef={messagesRef} onScroll={rememberScroll}
              onWheel={(event) => { if (event.deltaY !== 0 && !event.ctrlKey) cancelPendingRestore(); }}
              onTouchMove={cancelPendingRestore} onPointerDown={cancelPendingRestore}
              onKeyDown={(event) => {
                if (event.target === event.currentTarget && !event.defaultPrevented && !event.altKey && !event.ctrlKey && !event.metaKey
                  && ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) cancelPendingRestore();
              }} />
            <ChatComposer targetId={active.id} targetName={active.displayName} draft={draft}
              capability={capability} operation={operation} successHint={successHint} onDraftChange={onDraftChange} onSend={onSend} onRetry={onRetry} />
          </section>

          {infoExpanded && (
            <aside id={infoId} className="chat-workspace__info" aria-label="对象信息">
              <div className="chat-workspace__info-heading">
                <h2>对象信息</h2>
                <Button aria-label="收起信息" variant="ghost" className="chat-workspace__info-close"
                  onClick={() => onInfoExpandedChange(false)}>×</Button>
              </div>
              <AgentAvatar name={active.displayName} tone={active.avatarTone} size="lg" />
              <h3>{active.displayName}</h3>
              <p>{active.kind === 'human' ? '联系人' : 'Agent'} · {active.role}</p>
              <AvailabilityBadge availability={active.availability} />
              {targetDetail && <p>{targetDetail}</p>}
              <div className="chat-workspace__capabilities">
                <h3>能力</h3>
                {active.capabilities.length > 0 ? <ul>{active.capabilities.map((value) => <li key={value}>{value}</li>)}</ul> : <p>暂无能力说明。</p>}
              </div>
              {active.kind === 'agent' && <Button disabled={!onManageTarget} onClick={() => onManageTarget?.(active.id)}>管理 Agent</Button>}
            </aside>
          )}
        </>
      )}
    </section>
  );
}
