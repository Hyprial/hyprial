import { useChatContractRuntime } from './features/agent-chat/api/use-contract-runtime';
import type { AgentChatScenario } from './features/agent-chat/api/mock';
import { createFixtureFirstRunActions, type AccountLoginScenario } from './dev-mode/first-run-capabilities';
import { GuiCapabilitiesProvider, type GuiCapabilities } from './app/capabilities';
import { useCallback, useMemo, useReducer, useRef, useState } from 'react';
import { AppShell } from './app/AppShell';
import type { LivePort } from './adapters/live';
import { useLiveRuntime } from './pages/live';
import { HttpStubOperations, type StubOperations } from './adapters/stub';
import { useChatSimulation } from './features/chat/useChatSimulation';
import { appReducer, createFixtureState } from './state/appReducer';
import type { GuiPage } from './state/gui-view-model';
import type { ChatSeed, FirstRunMode } from './state/types';
import type { OrgCopyHandler, OrgViewModel } from './pages/org/org-view-model';
import type { MachineSectionModel } from './features/network/machine-view-model';
import type { TopologyModel } from './features/network/topology-view-model';
import type { AgentSeed } from './state/agent-selectors';
import type { AgentMutationGateway } from './features/agents/AgentMutationGateway';
import type { LinkedSession, SessionAction } from './dev-mode/session/session-types';
import { createSessionAgentGateway } from './dev-mode/session/linked-gateways';
import {
  selectAgentSeed,
  selectChatSeed,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectChatChannel,
  selectChatTargets,
  selectMessageConnection,
  selectModelServicesModel,
  selectOrgModel,
  selectTopologyModel,
} from './dev-mode/session/session-selectors';
import type { DevModuleId } from './dev-mode';
import { createAgentProviderMock, syntheticProviderLoginFiles, type AgentProviderScenario } from './features/agent-provider/api/mock';

export interface AppProps {
  readonly capabilities?: GuiCapabilities;
  readonly agentProviderScenario?: AgentProviderScenario;
  readonly accountLoginScenario?: AccountLoginScenario;
  readonly agentChatScenario?: AgentChatScenario;
  /** Test/composition entry; the browser entry selects this per build. */
  readonly firstRunMode?: FirstRunMode;
  /** Explicit fixture composition suppresses the live backend. */
  readonly dataMode?: 'fixture';
  readonly liveClient?: LivePort;
  /** Test/composition injection for the unified placeholder adapter. */
  readonly stubClient?: StubOperations;
  /** Test/development chat data; the product entry passes nothing. */
  readonly chatSeed?: ChatSeed;
  /** Test/development organization data; the product entry passes nothing. */
  readonly orgModel?: OrgViewModel;
  /** Test/composition clipboard hook; the product entry uses the local copy. */
  readonly orgCopy?: OrgCopyHandler;
  /** Test/development Agent data; the product entry passes nothing. */
  readonly agentSeed?: AgentSeed;
  /** Test/development write gateway; the product entry keeps the stub one. */
  readonly agentGateway?: AgentMutationGateway;
  /** Test/development machine data; the product entry passes nothing. */
  readonly machineModel?: MachineSectionModel;
  /** Test/development topology data; the product entry passes nothing. */
  readonly topologyModel?: TopologyModel;
  /** Continuous development session; the product entry passes nothing. */
  readonly session?: LinkedSession;
  readonly onSessionAction?: (action: SessionAction) => void;
  readonly devLabels?: Readonly<Record<GuiPage, DevModuleId>>;
}

export function App({
  stubClient,
  capabilities,
  agentProviderScenario,
  accountLoginScenario,
  agentChatScenario,
  dataMode,
  liveClient,
  firstRunMode,
  chatSeed,
  orgModel,
  orgCopy,
  agentSeed,
  agentGateway,
  machineModel,
  topologyModel,
  session: developmentSession,
  onSessionAction: developmentAction,
  devLabels,
}: AppProps = {}) {
  const live = useLiveRuntime(dataMode === undefined && (liveClient !== undefined || import.meta.env.PROD ||
    (typeof __HYPRIAL_LIVE_ENABLED__ !== 'undefined' && __HYPRIAL_LIVE_ENABLED__)), liveClient);
  const chatContract = useChatContractRuntime(import.meta.env.DEV && dataMode === 'fixture' ? agentChatScenario : undefined);
  // Development session state and write gateways are never evidence about the
  // trusted host. Keep them outside live composition, including bootstrap.
  const session = live ? undefined : developmentSession;
  const onSessionAction = live ? undefined : developmentAction;
  const [state, dispatch] = useReducer(
    appReducer, undefined, () => createFixtureState(live ? 'unavailable' : firstRunMode, null, live ? undefined : chatSeed),
  );
  const [stub] = useState<StubOperations>(() => stubClient ?? new HttpStubOperations());

  useChatSimulation(state, dispatch, live === null && !chatContract);

  const routedDispatch = useSessionDispatch(
    session,
    onSessionAction,
    dispatch,
  );
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const sessionGateway = useRef(
    session && onSessionAction
      ? createSessionAgentGateway(() => sessionRef.current as LinkedSession, onSessionAction)
      : undefined,
  ).current;

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

  const fixtureCapabilities = useMemo(() => import.meta.env.DEV && dataMode === 'fixture'
    ? {
        firstRun: createFixtureFirstRunActions(effectiveState.firstRun.mode, routedDispatch, accountLoginScenario),
        accountLoginDisabled: accountLoginScenario === 'disabled',
        agentProvider: agentProviderScenario ? createAgentProviderMock(agentProviderScenario) : undefined,
        agentProviderLoginFiles: agentProviderScenario ? async () => syntheticProviderLoginFiles() : undefined,
      }
    : undefined, [dataMode, effectiveState.firstRun.mode, routedDispatch, agentProviderScenario, accountLoginScenario]);
  return (
    <GuiCapabilitiesProvider value={capabilities ?? fixtureCapabilities}>
    <AppShell
      firstRunScenario={import.meta.env.DEV && dataMode === 'fixture' ? 'demo' : 'live'}
      live={live}
      chatContract={chatContract}
      state={effectiveState}
      dispatch={routedDispatch}
      stub={stub}
      orgModel={session ? selectOrgModel(session) : live ? undefined : orgModel}
      session={session}
      orgCopy={orgCopy}
      agentSeed={live ? undefined : session ? selectAgentSeed(session) : agentSeed}
      agentGateway={live ? undefined : sessionGateway ?? agentGateway}
      onSessionAction={onSessionAction}
      machineModel={live ? undefined : session ? selectMachineModel(session) : machineModel}
      topologyModel={live ? undefined : session ? selectTopologyModel(session) : topologyModel}
      modelSection={live ? undefined : session ? selectModelServicesModel(session) : undefined}
      messageSection={
        live
          ? undefined
          : session
            ? {
                state: selectMessageConnection(session).state,
                boundAgentName:
                  session.agents.find(
                    (agent) => agent.id === session.messageConnectionAgentId,
                  )?.name ?? null,
                authorizeOutcome:
                  selectMessageConnection(session).authorizeOutcome,
              }
            : undefined
      }
      chatChannel={live ? undefined : session ? selectChatChannel(session) : undefined}
      messageAgents={
        live
          ? undefined
          : session
            ? session.agents.map((agent) => ({ id: agent.id, name: agent.name }))
            : undefined
      }
      messageDefaultAgentId={live ? null : session ? 'agent-squire' : null}
      devLabels={devLabels}
    />
    </GuiCapabilitiesProvider>
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
