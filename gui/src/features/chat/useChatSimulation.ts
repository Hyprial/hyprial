import { useEffect } from 'react';
import type { Dispatch } from 'react';
import type { AppAction, AppState } from '../../state/types';
import { CHAT_SEND_DELAY_MS } from './chat-copy';

/**
 * Resolve outgoing messages that are waiting for a local outcome.
 *
 * The fixture surface resolves pending messages only when an explicit preview
 * or test composition supplies a local delivery mode. Live composition disables
 * this hook and consumes the host controller instead.
 * The timer is the only source of the resolved state; nothing here reports a
 * delivery on its own.
 */
export function useChatSimulation(
  state: AppState,
  dispatch: Dispatch<AppAction>,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled || state.chatSendMode === 'unavailable') {
      return;
    }
    const outcome = state.chatSendMode === 'reply' ? 'sent' : 'failed';
    const timers: number[] = [];

    for (const [targetId, thread] of Object.entries(state.threads)) {
      for (const message of thread) {
        if (message.delivery !== 'pending') {
          continue;
        }
        timers.push(
          window.setTimeout(() => {
            dispatch({
              type: 'chat-resolve',
              targetId,
              messageId: message.id,
              outcome,
            });
          }, CHAT_SEND_DELAY_MS),
        );
      }
    }

    return () => {
      for (const timer of timers) {
        window.clearTimeout(timer);
      }
    };
  }, [state.threads, state.chatSendMode, dispatch, enabled]);
}
