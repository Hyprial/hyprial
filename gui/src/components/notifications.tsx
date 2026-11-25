/*
 * Decision record (unavailable copy to notifications, PR 1f/F0, 2026-11-14):
 * - Owner rule: a function that is unavailable is not explained on the page.
 *   Without a capability port the control is not rendered; with a port whose
 *   call is refused the control stays operable and the refusal is reported
 *   when the user operates it, through the regular toast and notification
 *   center (state/appReducer push-notice).
 * - The provider is mounted by the shared AppShell, so preview and formal
 *   entries behave identically. This module reads no build mode and no dev
 *   token and imports state/types only.
 * - The unavailable text is `${subject}暂不可用。` with warning tone; the
 *   toast leaves after the usual delay (autoDismiss) while the warning entry
 *   stays in the notification center, so it never becomes a permanent
 *   toast over the corner of the screen. A technical reason or code is
 *   never put into the toast text; it is the entry source shown by the
 *   notification center (`source · code`).
 * - Without a provider the notify function throws: a missing provider must
 *   fail a test instead of swallowing the message.
 */
import { createContext, useCallback, useContext, type Dispatch, type ReactNode } from 'react';
import type { AppAction, NoticeTone } from '../state/types';

export interface NotifyInput {
  readonly tone: NoticeTone;
  readonly text: string;
  readonly source?: string;
  readonly autoDismiss?: boolean;
}

export type Notify = (input: NotifyInput) => void;

export interface UnavailableDetail {
  readonly code?: string;
  readonly reason?: string;
  readonly source?: string;
}

const MISSING_PROVIDER = 'notifications provider is missing';

const NotifyContext = createContext<Notify | null>(null);

export function NotificationsProvider({
  dispatch,
  children,
}: {
  readonly dispatch: Dispatch<AppAction>;
  readonly children: ReactNode;
}) {
  const notify = useCallback<Notify>(
    (input) => dispatch({ type: 'push-notice', ...input }),
    [dispatch],
  );
  return <NotifyContext.Provider value={notify}>{children}</NotifyContext.Provider>;
}

export function useNotify(): Notify {
  const notify = useContext(NotifyContext);
  return (
    notify ??
    (() => {
      throw new Error(MISSING_PROVIDER);
    })
  );
}

export function unavailableText(subject: string): string {
  return `${subject}暂不可用。`;
}

export function unavailableNotice(subject: string, detail: UnavailableDetail = {}): NotifyInput {
  const { source, code } = detail;
  const origin = source && code ? `${source} · ${code}` : (source ?? code ?? subject);
  return { tone: 'warning', text: unavailableText(subject), source: origin, autoDismiss: true };
}

export function useNotifyUnavailable(): (subject: string, detail?: UnavailableDetail) => void {
  const notify = useNotify();
  return useCallback(
    (subject, detail) => notify(unavailableNotice(subject, detail)),
    [notify],
  );
}
