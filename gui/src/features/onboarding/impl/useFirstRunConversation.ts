import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import type { ChatController } from '../../agent-chat';
import type { FirstRunConversationRuntime } from './types';

const noopSubscribe = () => () => {};

/**
 * Decision block (brief-firstrun-bind, 2026-11-03):
 * - Defect: a confirmed success in the shared chat controller from an ordinary
 *   chat send (or an earlier session / restored pending request) flipped the
 *   first-run runtime to sendAccepted on mount, so opening first-run from
 *   Settings completed the flow while it still showed the account step.
 * - Decision: `sendAccepted` requires a send armed by THIS flow instance
 *   (onSend below) and a request id different from the one captured at arm
 *   time. The ChatController only mints a new id through this armed send,
 *   so any pre-existing success (chat, earlier session, another tab,
 *   restored pending) can never complete first-run; the flow's own send —
 *   including its retry and its lookup confirmation — still completes it.
 *
 * Projects the shared chat controller onto the first-run conversation step.
 * The controller keeps the draft, the operation outcome and the real history;
 * this hook only derives view state and never manufactures success.
 */
export function useFirstRunConversation(
  chat: ChatController | null,
  sourceLabel?: string,
): FirstRunConversationRuntime | null {
  const ownSendRef = useRef({ armed: false, foreignRequestId: null as string | null });
  const snapshot = useSyncExternalStore(
    chat ? chat.subscribe : noopSubscribe,
    chat ? chat.getSnapshot : () => null,
  );
  const ready = snapshot?.status === 'ready';
  const targetId = snapshot?.activeTargetId ?? null;
  const target = targetId && snapshot
    ? snapshot.targets.find((item) => item.id === targetId)
    : undefined;
  const entry = targetId && snapshot ? snapshot.entries[targetId] : undefined;
  const operation = entry?.operation;
  const armed = ownSendRef.current.armed;
  const sendAccepted = armed
    && operation?.status === 'success'
    && operation.confirmationPending !== true
    && operation.requestId !== ownSendRef.current.foreignRequestId;
  const operationStatus = operation?.status;

  useEffect(() => {
    if (!chat) return;
    void chat.start();
  }, [chat]);

  useEffect(() => {
    if (!chat || !ready || !targetId) return;
    void chat.select(targetId);
  }, [chat, ready, targetId]);

  useEffect(() => {
    if (!chat || !targetId || operationStatus !== 'unknown') return;
    let alive = true;
    let attempts = 0;
    const timer = setInterval(() => {
      if (!alive || attempts >= 20) {
        if (alive) clearInterval(timer);
        return;
      }
      attempts += 1;
      void chat.reconcile(targetId);
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [chat, targetId, operationStatus]);

  return useMemo(() => {
    if (!chat || !snapshot) return null;
    const sendable = ready && !!entry?.sessionId && !!entry.conversationId
      && target?.availability === 'available';
    return {
      status: snapshot.status === 'error' ? 'error' : snapshot.status === 'loading' ? 'loading' : 'ready',
      error: snapshot.error,
      sourceLabel,
      targetId,
      targetName: target?.displayName ?? '未选择对象',
      draft: targetId ? snapshot.drafts[targetId] ?? '' : '',
      messages: entry?.messages ?? [],
      history: { status: entry?.historyStatus ?? 'loading', error: entry?.historyError },
      operation,
      sendAccepted,
      capability: sendable && !operation?.confirmationPending
        ? 'ready'
        : snapshot.status === 'error' ? 'unavailable' : 'unconfirmed',
      onDraftChange: (value: string) => {
        if (targetId) chat.setDraft(targetId, value);
      },
      onSend: (text: string) => {
        if (!targetId) return;
        ownSendRef.current = { armed: true, foreignRequestId: operation?.requestId ?? null };
        void chat.send(targetId, text);
      },
      onRetry: () => {
        if (!targetId || !operation) return;
        if (operation.status === 'definitive-failure') {
          if (operation.canRetry) void chat.retry(operation.requestId);
          else chat.resetFailure(targetId);
        } else if (operation.status === 'unknown') void chat.reconcile(targetId);
      },
      onCheckResult: () => {
        if (targetId) void chat.reconcile(targetId);
      },
    };
  }, [chat, snapshot, sourceLabel, targetId, target, entry, operation, sendAccepted, ready, armed]);
}
