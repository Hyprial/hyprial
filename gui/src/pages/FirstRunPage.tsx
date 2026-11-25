import type { Dispatch } from 'react';
import { useGuiCapabilities } from '../app/capabilities';
import { useAccountSettings } from '../features/account';
import { FirstRunOnboarding, useFirstRunConversation, type FirstRunScenario } from '../features/onboarding';
import type { ChatPageRuntime } from './live/LiveChatPage';
import type { AppAction } from '../state/types';

interface FirstRunPageProps {
  readonly scenario?: FirstRunScenario;
  readonly dispatch: Dispatch<AppAction>;
  readonly resumeFromStatus?: boolean;
  /** Shared chat contract runtime; the first message reuses the chat page controller. */
  readonly chat?: ChatPageRuntime | null;
}

export function FirstRunPage({ dispatch, scenario = 'live', chat, resumeFromStatus }: FirstRunPageProps) {
  const { firstRun: actions } = useGuiCapabilities();
  const account = useAccountSettings(actions);
  const conversation = useFirstRunConversation(chat?.chat ?? null, chat?.sourceLabel);
  const targetId = conversation?.targetId ?? null;
  return (
    <section className="page page-first-run" data-page="first-run">
      <FirstRunOnboarding
        actions={actions}
        scenario={scenario}
        resumeFromStatus={resumeFromStatus}
        conversation={conversation ?? undefined}
        onOpenChat={targetId
          ? () => dispatch({ type: 'open-agent-chat', targetId })
          : undefined}
        onOpenReplyChat={(agentUri) => dispatch({ type: 'open-agent-chat', targetId: agentUri })}
        onComplete={() => {
          void account.run('read');
          dispatch({ type: 'first-run-complete' });
        }}
        onExit={() => dispatch({ type: 'navigate', page: 'settings' })}
      />
    </section>
  );
}
