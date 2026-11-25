import { useCallback, useMemo, useReducer, useRef } from 'react';
import { App } from '../../src/App';
import { DEMO_BASELINE } from './demo-baseline';
import { createInitialSession } from '../../src/dev/session/initial-session';
import {
  applyJ4Scenario,
  applyJ10Scenario,
  type J4Scenario,
  type J10Scenario,
} from '../../src/dev/remaining-scenarios';
import { createSessionAgentGateway } from '../../src/dev/session/linked-gateways';
import { SessionToolbar } from '../../src/dev/SessionToolbar';
import { createFixtureFirstRunActions } from '../../src/dev/first-run-capabilities';
import { useChatContractRuntime } from '../../src/dev/mock/use-contract-runtime';
import { sessionReducer } from '../../src/dev/session/session-reducer';
import type { LinkedSession, SessionAction } from '../../src/dev/session/session-types';
import type { AppAction } from '../../src/state/types';
import {
  selectAgentSeed,
  selectChatSeed,
  selectChatTargets,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectOrgModel,
  selectTopologyModel,
} from '../../src/dev/session/session-selectors';

/**
 * The linked development composition as a unit-test harness: it mirrors the
 * entry's linked mode (session-backed wizard and section models) without the
 * floating tools or the hash-driven store, so product-flow tests stay
 * deterministic.
 */
export function LinkedPreviewComposition({
  j4 = 'ready',
  j10 = 'ready',
}: {
  readonly j4?: J4Scenario;
  readonly j10?: J10Scenario;
} = {}) {
  const [session, dispatch] = useReducer(sessionReducer, undefined, () =>
    applyJ4Scenario(applyJ10Scenario(createInitialSession(), j10), j4),
  );
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const dispatchSession = (action: SessionAction) => dispatch(action);
  const sessionGateway = useMemo(
    () => createSessionAgentGateway(() => sessionRef.current as LinkedSession, dispatchSession),
    [],
  );
  const sessionFirstRun = selectFirstRunState(session);
  const wizardMode = sessionFirstRun.mode;
  const firstRunChat = useChatContractRuntime(wizardMode === 'conversation-failure' ? 'failure' : 'success');
  const mockCapabilitiesFactory = useCallback(
    (dispatch: React.Dispatch<AppAction>) =>
      Object.freeze({
        firstRun: createFixtureFirstRunActions(wizardMode, dispatch),
        firstRunWizardEnabled: true,
      }),
    [wizardMode],
  );
  return (
    <>
    <App
      baseline={DEMO_BASELINE}
      capabilitiesFactory={mockCapabilitiesFactory}
      firstRunChat={firstRunChat}
      stateSeed={{
        firstRun: selectFirstRunState(session),
        targets: selectChatTargets(session),
        connections: selectConnections(session),
        threads: selectChatSeed(session)?.threads,
      }}
      orgModel={selectOrgModel(session)}
      agentSeed={selectAgentSeed(session)}
      agentGateway={sessionGateway}
      machineModel={selectMachineModel(session)}
      topologyModel={selectTopologyModel(session)}
      onFirstRunAction={dispatchSession ? (action) => dispatchSession(action as SessionAction) : undefined}
    />
      <SessionToolbar session={session} onAction={dispatchSession} />
    </>
  );
}
