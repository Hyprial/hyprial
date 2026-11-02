import type { Dispatch } from 'react';
import { useGuiCapabilities } from '../app/capabilities';
import type { StubOperations } from '../adapters/stub';
import { FirstRunOnboarding, type FirstRunScenario } from '../features/onboarding';
import type { AppState, AppAction } from '../state/types';

interface FirstRunPageProps {
  readonly state?: AppState;
  readonly scenario?: FirstRunScenario;
  readonly dispatch: Dispatch<AppAction>;
  /**
   * Retained for the existing AppShell contract. First-run uses shared capabilities and never sends
   * account or credential commands through the placeholder business adapter.
   */
  readonly stub?: StubOperations;
}

export function FirstRunPage({ dispatch, state, scenario = 'live' }: FirstRunPageProps) {
  const { firstRun: actions } = useGuiCapabilities();
  return (
    <section className="page page-first-run" data-page="first-run">
      <FirstRunOnboarding
        actions={actions}
        scenario={scenario}
        conversationPreview={scenario === 'demo' ? { onConfirm: (text) => {
          if (state?.firstRun.mode === 'conversation-failure') throw new Error('Simulated handoff failure');
          dispatch({ type: 'first-run-submit', step: 'conversation' });
          dispatch({ type: 'first-run-succeed', step: 'conversation', previewOnly: true });
          dispatch({ type: 'open-chat-draft', targetId: 'agent-squire', draft: text });
        } } : undefined}
        onExit={() => dispatch({ type: 'navigate', page: 'settings' })}
      />
    </section>
  );
}
