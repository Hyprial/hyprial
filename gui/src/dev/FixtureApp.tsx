/**
 * Decision record (GUI deep modules C3, 2026-11-08): development composition
 * root. The fixture state, contract mocks, legacy scenario seeds and the
 * linked development session that used to be composed inside the formal `App`
 * are concentrated here; `DevPreviewApp`, `OrgDevApp` and tests that need
 * fixture data render this component. Non-fixture branches (live runtime,
 * plain unavailable) delegate to the formal `App`, so preview and product
 * share one shell and one set of pages and no presentation is duplicated.
 * Composition props are fixed per mount: entries remount through React keys
 * when the scenario changes, and the branch decision itself uses no hooks.
 */
import type { AgentChatScenario } from './mock/agent-chat';
import { useChatContractRuntime } from './mock/use-contract-runtime';
import { createFixtureFirstRunActions, type AccountLoginScenario } from './first-run-capabilities';
import { GuiCapabilitiesProvider, type GuiCapabilities } from '../app/capabilities';
import { useCallback, useMemo, useReducer, useRef } from 'react';
import { AppShell, useFirstRunRouteGate } from '../app/AppShell';
import { BackendProvider } from '../app/backend';
import type { LivePort } from '../adapters/live';
import { App } from '../App';
import { useChatSimulation } from '../features/chat';
import { appReducer, createEmptyState } from '../state/appReducer';
import { createFixtureState, fixtureAppReducer } from './fixtures/fixture-state';
import type { ChatSeed, FirstRunMode } from '../state/types';
import type { OrgCopyHandler, OrgViewModel } from '../pages/org/org-view-model';
import type { MachineSectionModel, TopologyModel } from '../features/network';
import type { AgentSeed } from '../state/agent-selectors';
import type { AgentMutationGateway } from '../features/agents';
import type { LinkedSession, SessionAction } from './session/session-types';
import { createSessionAgentGateway } from './session/linked-gateways';
import { sessionReducer } from './session/session-reducer';
import { LINKED_ACTION_REFUSED, legacyOrgSource } from './legacy-org-source';
import {
  selectAgentSeed,
  selectChatSeed,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectChatTargets,
  selectOrgModel,
  selectTopologyModel,
} from './session/session-selectors';
import { syntheticProviderLoginFiles, type AgentProviderScenario } from './mock/agent-provider';
import { createDevSettingsApis } from './mock/settings';

export interface FixtureAppProps {
  readonly capabilities?: GuiCapabilities;
  readonly liveClient?: LivePort;
  /** Test/composition injection for the unified placeholder adapter. */
  readonly agentProviderScenario?: AgentProviderScenario;
  readonly accountLoginScenario?: AccountLoginScenario;
  readonly agentChatScenario?: AgentChatScenario;
  /** Test/composition entry; the browser entry selects this per build. */
  readonly firstRunMode?: FirstRunMode;
  /** Explicit first-run conversation contract selection. */
  readonly firstRunChatScenario?: AgentChatScenario;
  /** Explicit fixture composition suppresses the live backend. */
  readonly dataMode?: 'fixture';
  /** Test/development chat data. */
  readonly chatSeed?: ChatSeed;
  /** Test/development organization data. */
  readonly orgModel?: OrgViewModel;
  /** Test/composition clipboard hook. */
  readonly orgCopy?: OrgCopyHandler;
  /** Test/development Agent data. */
  readonly agentSeed?: AgentSeed;
  /** Test/development write gateway. */
  readonly agentGateway?: AgentMutationGateway;
  /** Test/development machine data. */
  readonly machineModel?: MachineSectionModel;
  /** Test/development topology data. */
  readonly topologyModel?: TopologyModel;
  /** Continuous development session. */
  readonly session?: LinkedSession;
  readonly onSessionAction?: (action: SessionAction) => void;
}

export function FixtureApp(props: FixtureAppProps = {}) {
  const { capabilities, liveClient, dataMode, session } = props;
  // A live runtime or an explicitly non-fixture composition keeps the formal
  // behaviour: no session, no seeds, no fallback data. The development
  // session and write gateways are never evidence about the trusted host.
  const fixture = import.meta.env.DEV && dataMode === 'fixture';
  const liveComposition = dataMode === undefined && liveClient !== undefined;
  if (liveComposition || (!fixture && session === undefined)) {
    return <App capabilities={capabilities} liveClient={liveClient} />;
  }
  return <SessionFixtureComposition {...props} fixture={fixture} />;
}

function SessionFixtureComposition({
  capabilities,
  agentProviderScenario,
  accountLoginScenario,
  agentChatScenario,
  firstRunChatScenario: firstRunChatScenarioProp,
  fixture,
  firstRunMode,
  chatSeed,
  orgModel,
  orgCopy,
  agentSeed,
  agentGateway,
  machineModel,
  topologyModel,
  session,
  onSessionAction,
}: FixtureAppProps & { readonly fixture: boolean }) {
  const chatContract = useChatContractRuntime(fixture ? agentChatScenario : undefined);
  const firstRunChatScenario = firstRunChatScenarioProp
    ?? (fixture && firstRunMode === 'conversation-failure' ? 'failure' : undefined);
  const firstRunChatContract = useChatContractRuntime(fixture ? firstRunChatScenario : undefined);
  const settingsApis = useMemo(() => createDevSettingsApis(), []);
  const [state, dispatch] = useReducer(
    fixture ? fixtureAppReducer : appReducer,
    undefined,
    () => fixture
      ? createFixtureState(firstRunMode, null, chatSeed)
      : createEmptyState('unavailable'),
  );

  useChatSimulation(state, dispatch, fixture && !chatContract);

  const routedDispatch = useSessionDispatch(
    session,
    onSessionAction,
    dispatch,
  );
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const sessionActionRef = useRef(onSessionAction);
  sessionActionRef.current = onSessionAction;
  const sessionGateway = useRef(
    session && onSessionAction
      ? createSessionAgentGateway(() => sessionRef.current as LinkedSession, onSessionAction)
      : undefined,
  ).current;

  const fixtureOrgModel = useMemo(
    () => session ? selectOrgModel(session) : orgModel,
    [
      session?.org,
      session?.members,
      session?.invites,
      session?.activeViewer,
      session?.firstRun.error,
      orgModel,
    ],
  );
  const fixtureOrgSource = useMemo(() => fixture
    ? legacyOrgSource(
        fixtureOrgModel,
        (action) => sessionActionRef.current?.(action),
        (action) => {
          const current = sessionRef.current;
          if (!current) return undefined;
          const next = sessionReducer(current, action);
          if (next === current) return LINKED_ACTION_REFUSED;
          if (action.type === 'org-join' && next.firstRun.error) return next.firstRun.error;
          return undefined;
        },
        session?.org?.name,
        () => sessionRef.current,
      )
    : undefined, [fixture, fixtureOrgModel, session?.org?.name]);

  const linkedFirstRun = session ? selectFirstRunState(session) : null;
  const linkedChat = session ? selectChatSeed(session) : chatSeed;
  const effectiveState =
    session && linkedFirstRun
      ? {
          ...state,
          firstRun: linkedFirstRun,
          threads: linkedChat?.threads ?? state.threads,
          connections: session ? selectConnections(session) : state.connections,
          targets: session ? selectChatTargets(session) : state.targets,
        }
      : state;

  const firstRunActions = useMemo(() => createFixtureFirstRunActions(
    effectiveState.firstRun.mode, routedDispatch,
    accountLoginScenario ?? (effectiveState.firstRun.mode === 'complete' ? 'ready' : undefined),
  ), [effectiveState.firstRun.mode, routedDispatch, accountLoginScenario]);
  const fixtureCapabilities = useMemo<GuiCapabilities | undefined>(() => fixture
    ? {
        firstRun: firstRunActions,
        ...settingsApis,
        agentProviderLoginFiles: agentProviderScenario ? async () => syntheticProviderLoginFiles() : undefined,
        orgSource: fixtureOrgSource,
        orgSourceKind: fixtureOrgSource === undefined ? undefined : 'isolated',
      }
    : undefined, [fixture, firstRunActions, agentProviderScenario, accountLoginScenario, fixtureOrgSource, settingsApis]);
  const effectiveCapabilities = useMemo<GuiCapabilities | undefined>(
    () => capabilities ?? fixtureCapabilities,
    [capabilities, fixtureCapabilities],
  );
  // A7 route gate: the first-run wizard is hidden in the connected product.
  // A composition may opt in explicitly; otherwise only non-connected
  // compositions (development/test journeys) keep the historical entry.
  const firstRunWizardEnabled = effectiveCapabilities?.firstRunWizardEnabled ?? true;
  const gatedDispatch = useFirstRunRouteGate(firstRunWizardEnabled, routedDispatch);
  return (
    <BackendProvider value={{ status: 'unavailable' }}>
    <GuiCapabilitiesProvider value={effectiveCapabilities}>
    <AppShell
      firstRunScenario={fixture ? 'isolated' : 'live'}
      chatContract={chatContract}
      firstRunChat={firstRunChatContract}
      state={effectiveState}
      dispatch={gatedDispatch}
      firstRunWizardEnabled={firstRunWizardEnabled}
      orgModel={session ? selectOrgModel(session) : orgModel}
      orgCopy={orgCopy}
      agentSeed={session ? selectAgentSeed(session) : agentSeed}
      agentGateway={sessionGateway ?? agentGateway}
      machineModel={session ? selectMachineModel(session) : machineModel}
      topologyModel={session ? selectTopologyModel(session) : topologyModel}
    />
    </GuiCapabilitiesProvider>
    </BackendProvider>
  );
}

/**
 * With a linked session, business actions become session actions; presentation
 * actions keep using the existing reducer.
 */
function useSessionDispatch(
  session: LinkedSession | undefined,
  onSessionAction: ((action: SessionAction) => void) | undefined,
  dispatch: React.Dispatch<Parameters<typeof appReducer>[1]>,
) {
  // Refs keep the returned dispatch stable: the first-run page schedules timers
  // from an effect that depends on dispatch identity.
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const handlerRef = useRef(onSessionAction);
  handlerRef.current = onSessionAction;

  return useCallback(
    (action: Parameters<typeof appReducer>[1]) => {
      if (
        sessionRef.current &&
        handlerRef.current &&
        action.type.startsWith('first-run-')
      ) {
        handlerRef.current(action as unknown as SessionAction);
        return;
      }
      dispatch(action);
    },
    [dispatch],
  );
}
