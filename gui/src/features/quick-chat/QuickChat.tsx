import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { Dispatch, KeyboardEvent } from 'react';
import { AgentAvatar } from '../../components/AgentAvatar';
import { mergeMessagesById } from '../../state/messages';
import {
  getFailedMessage,
  getPendingMessage,
} from '../../state/selectors';
import type { AppAction, AppState } from '../../state/types';
import { ChatComposer } from '../chat/ChatComposer';
import { ChatMessages } from '../chat/ChatMessages';
import { getChatUnavailableNotice } from '../chat/chat-copy';
import { QUICK_CHAT_TARGET_ID } from './quick-chat-constants';

interface QuickChatProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
}

/**
 * Squire quick chat: one launcher button plus a non-modal panel.
 *
 * The panel reads and writes the same conversation and draft as the chat page,
 * so a message sent here is the message the chat page shows.
 */
export function QuickChat({ state, dispatch }: QuickChatProps) {
  const open = state.quickChatOpen;
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const wasOpenRef = useRef(open);
  const [placement, setPlacement] = useState<{
    readonly bottom: number;
    readonly maxHeight: number;
  } | null>(null);
  const target =
    state.targets.find((candidate) => candidate.id === QUICK_CHAT_TARGET_ID) ??
    state.targets[0];

  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    const measure = () => {
      const viewportHeight = window.innerHeight;
      const nav = document.querySelector('.nav');
      const navBottom =
        nav instanceof HTMLElement
          ? Math.round(nav.getBoundingClientRect().bottom)
          : 0;
      const composer = document.querySelector('.page-chat .composer-wrap');
      const composerTop =
        composer instanceof HTMLElement
          ? Math.round(composer.getBoundingClientRect().top)
          : null;
      const composerHasRoom =
        composerTop !== null && composerTop > navBottom + 220;
      const panelBottom = composerHasRoom
        ? Math.min(composerTop - 12, viewportHeight - 18)
        : viewportHeight - 18;
      const topLimit = navBottom + 8;
      const available = panelBottom - topLimit;
      setPlacement({
        bottom: Math.max(18, viewportHeight - panelBottom),
        maxHeight: Math.max(96, Math.min(460, available)),
      });
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [open, state.page]);

  useEffect(() => {
    if (wasOpenRef.current === open) {
      return;
    }
    wasOpenRef.current = open;
    if (open) {
      panelRef.current?.querySelector('input')?.focus({ preventScroll: true });
      return;
    }
    buttonRef.current?.focus();
  }, [open]);

  const close = () => dispatch({ type: 'set-quick-chat', open: false });

  if (!target) {
    return null;
  }

  const thread = mergeMessagesById(state.threads[target.id] ?? []);
  const draft = state.drafts[target.id] ?? '';
  const canSend = target.availability === 'available';
  const unavailableNotice = getChatUnavailableNotice(target);
  const pending = getPendingMessage(state, target.id) !== undefined;
  const failedMessage = getFailedMessage(state, target.id);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape' || !open) {
      return;
    }
    event.stopPropagation();
    close();
  };

  return (
    <div className="quick-chat" onKeyDown={handleKeyDown}>
      <button
        ref={buttonRef}
        type="button"
        className="quick-chat-button"
        aria-label={`打开 ${target.displayName} 快捷对话`}
        aria-expanded={open}
        aria-controls="quick-chat-panel"
        onClick={() => dispatch({ type: 'set-quick-chat', open: !open })}
      >
        快捷对话
      </button>

      {open && (
        <section
          ref={panelRef}
          id="quick-chat-panel"
          className="quick-chat-panel"
          style={
            placement
              ? {
                  bottom: `${placement.bottom}px`,
                  maxHeight: `${placement.maxHeight}px`,
                }
              : undefined
          }
          role="dialog"
          aria-modal="false"
          aria-label={`${target.displayName} 快捷对话`}
        >
          <header className="quick-chat-head">
            <AgentAvatar
              name={target.displayName}
              tone={target.avatarTone}
              size="md"
            />
            <div className="quick-chat-title">
              <h2>{target.displayName}</h2>
              <span>{target.role}</span>
            </div>
            <button
              type="button"
              className="icon-button quick-chat-close"
              aria-label="关闭快捷对话"
              onClick={close}
            >
              ×
            </button>
          </header>

          {unavailableNotice && (
            <div
              className="chat-unavailable"
              id="quick-chat-unavailable"
              role="note"
            >
              {unavailableNotice}
            </div>
          )}

          <ChatMessages
            messages={thread}
            targetName={target.displayName}
            ariaLabel={`快捷对话与 ${target.displayName} 的消息`}
          />
          <ChatComposer
            idPrefix="quick-chat"
            label={`给 ${target.displayName} 的快捷消息`}
            draft={draft}
            canSend={canSend}
            pending={pending}
            failed={failedMessage !== undefined}
            noticeId={
              unavailableNotice ? 'quick-chat-unavailable' : undefined
            }
            onDraftChange={(value) =>
              dispatch({ type: 'set-draft', targetId: target.id, value })
            }
            onSend={() =>
              dispatch({
                type: 'chat-send',
                targetId: target.id,
                text: draft,
              })
            }
            onRetry={() => {
              if (!failedMessage) {
                return;
              }
              dispatch({
                type: 'chat-retry',
                targetId: target.id,
                messageId: failedMessage.id,
              });
            }}
          />

          <button
            type="button"
            className="button button-secondary quick-chat-view"
            onClick={() =>
              dispatch({ type: 'open-agent', agentId: target.id })
            }
          >
            查看 Agent
          </button>
        </section>
      )}
    </div>
  );
}
