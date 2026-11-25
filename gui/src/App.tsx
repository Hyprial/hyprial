/**
 * Decision record (WP1 product composition, 2026-11-09; deep modules
 * 2026-11-10; artifact-neutral composition inputs 2026-11-10):
- `App` is the single product composition for every entry. It reads no build
  mode, imports no dev-only module and keeps the deep-module rule that the
  formal composition renders only connected, live and unavailable behaviours.
- Composition inputs are named for what they mean, not where they come from:
  `initialState` (a canonical reducer seed), `baseline` (+ `chatSeed`,
  `firstRunMode`) for data-driven state construction, and
  `capabilitiesFactory` to build a capability set around this app's action
  dispatch. The product entry passes none of them; development and parity
  compositions inject their data through these neutral names, so the release
  bundle carries no mock-composition identifiers (the production copy gate
  scans dist content and rejects them). Default/mock data itself stays under
  `src/dev/` only.
- Seeding is presence-driven: a composition that supplies any of the inputs
  above without a backend and without a `liveClient` renders from that input.
  A `liveClient` takes priority over injected seeds — the trusted live
  runtime is never mixed with composition data — and connected capabilities
  are merged over injected ones.
- Capability objects decide behaviour: `firstRunWizardEnabled` comes from the
  injected capabilities alone (connected capabilities carry `false`, seeded
  compositions default to `true`, an absent field reads as enabled).
- Chat contract runtimes (`chatContract`, `firstRunChat`) are composition
  inputs; the composition root creates them with its own scenario data.
- `stateSeed` overlays composition-owned presentation state (the development
  preview's linked session) onto the reducer state; `onFirstRunAction` routes
  first-run actions back to that owner. Both are ignored inside a live
  composition: development session state is never evidence about the trusted
  host. The reducer keeps authority over everything the composition did not
  inject.
- Deterministic org/agent/network/model/message data is injected per
  composition; a live runtime suppresses every injected seed.
- Merge integration (2026-11-10): compositions may pin the initial route via
  `initialPage`. It seeds only `state.page`, never business data, so a
  connected backend or a trusted live runtime keeps full data authority; the
  settings-convergence reference slice and live first-run compositions use it
  because the converged settings page no longer carries a wizard entry.
- Journey integration (2026-11-12): apply injected presentation state before
  reducing an action as well as before rendering. A newly created target must
  exist when the reducer handles its first draft/send action. Live runtimes
  still ignore all injected presentation state.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, type Dispatch } from 'react';
import { AppShell, useFirstRunRouteGate } from './app/AppShell';
import { BackendProvider, type BackendState } from './app/backend';
import { createConnectedGuiCapabilities, GuiCapabilitiesProvider, type GuiCapabilities } from './app/capabilities';
import type { LivePort } from './adapters/live';
import { useLiveRuntime } from './pages/live';
import type { ChatPageRuntime } from './pages/live/LiveChatPage';
import { useChatSimulation } from './features/chat';
import { firstRunWizardOffered } from './features/onboarding';
import { appReducer, applyStateSeed, createEmptyState, createStateFromBaseline, type StateBaseline } from './state/appReducer';
import type {
  AppAction,
  AppState,
  ChatSeed,
  FirstRunMode,
  StateSeed,
} from './state/types';
import type { OrgCopyHandler, OrgViewModel } from './pages/org/org-view-model';
import type { MachineSectionModel, TopologyModel } from './features/network';
import type { AgentSeed } from './state/agent-selectors';
import type { AgentMutationGateway } from './features/agents';
import type { GuiPage } from './state/gui-view-model';
import type { StartSummary } from './pages/start/start-summary';

export interface AppProps {
  readonly backend?: BackendState;
  readonly capabilities?: GuiCapabilities;
  readonly liveClient?: LivePort;
  /** Test/composition injection for the unified placeholder adapter. */
  /**
   * Composition-only initial route: seeds `state.page` before any user
   * navigation. It never seeds business data, so a connected backend or a
   * trusted live runtime keeps full data authority.
   */
  readonly initialPage?: GuiPage;
  readonly onPageChange?: (page: GuiPage) => void;
  /**
   * Canonical reducer seed built by the composition root. Tests may instead
   * supply baseline data and a chat seed; the product entry supplies neither.
   */
  readonly initialState?: AppState;
  readonly firstRunMode?: FirstRunMode;
  /** Baseline presentation data for a data-driven composition. */
  readonly baseline?: StateBaseline;
  readonly chatSeed?: ChatSeed;
  /**
   * Builds the capability set around this app's action dispatch. The
   * composition root closes over its own capability adapter; the product entry
   * passes nothing.
   */
  readonly capabilitiesFactory?: (dispatch: Dispatch<AppAction>) => GuiCapabilities;
  /** Chat page runtime supplied by the composition root. */
  readonly chatContract?: ChatPageRuntime | null;
  /** First-run-only chat runtime; ignored when the chat page runtime exists. */
  readonly firstRunChat?: ChatPageRuntime | null;
  /** Deterministic organization model supplied by a composition entry. */
  readonly orgModel?: OrgViewModel;
  /** Clipboard hook override; the shell copies locally by default. */
  readonly orgCopy?: OrgCopyHandler;
  /** Deterministic Agent data and gateway supplied by a composition entry. */
  readonly agentSeed?: AgentSeed;
  readonly agentGateway?: AgentMutationGateway;
  /** Deterministic machine/topology data supplied by a composition entry. */
  readonly machineModel?: MachineSectionModel;
  readonly topologyModel?: TopologyModel;
  /** Composition-owned Start-page counts; absent keeps the source explicit as unknown. */
  readonly startSummary?: StartSummary;
  /** Composition-owned presentation state overlaid onto the reducer state. */
  readonly stateSeed?: StateSeed;
  /** Receives first-run actions when a composition owns the first-run flow. */
  readonly onFirstRunAction?: (action: AppAction) => void;
}

export function App({
  backend,
  capabilities,
  liveClient,
  initialPage,
  onPageChange,
  initialState,
  firstRunMode,
  baseline,
  chatSeed,
  capabilitiesFactory,
  chatContract: chatContractProp,
  firstRunChat: firstRunChatProp,
  orgModel,
  orgCopy,
  agentSeed,
  agentGateway,
  machineModel,
  topologyModel,
  startSummary,
  stateSeed,
  onFirstRunAction,
}: AppProps = {}) {
  const connected = backend?.status === 'connected';
  const liveRequested = !connected && liveClient !== undefined;
  const live = useLiveRuntime(liveRequested, liveClient);
  const seeded = !connected && !liveRequested
    && (initialState !== undefined || baseline !== undefined || capabilitiesFactory !== undefined);
  const chatContract = chatContractProp ?? null;
  const firstRunChat = firstRunChatProp ?? null;
  const [state, dispatch] = useReducer(
    (current: AppState, action: AppAction) => appReducer(
      applyStateSeed(current, seeded ? stateSeed : undefined), action,
    ),
    undefined,
    () => {
      const base = connected
        ? createEmptyState('unavailable')
        : seeded
          ? (initialState ?? (baseline
            ? createStateFromBaseline(baseline, chatSeed, firstRunMode)
            : createEmptyState('unavailable')))
          : live !== null
            ? createEmptyState('live')
            : createEmptyState('unavailable');
      return initialPage === undefined ? base : { ...base, page: initialPage };
    },
  );
  useEffect(() => { onPageChange?.(state.page); }, [onPageChange, state.page]);

  useChatSimulation(state, dispatch, seeded && live === null && chatContract === null);

  const onFirstRunActionRef = useRef(onFirstRunAction);
  onFirstRunActionRef.current = onFirstRunAction;
  const liveRef = useRef(live);
  liveRef.current = live;
  const routedDispatch = useCallback<typeof dispatch>((action) => {
    if (!liveRef.current && onFirstRunActionRef.current && action.type.startsWith('first-run-')) {
      onFirstRunActionRef.current(action);
      return;
    }
    dispatch(action);
  }, [dispatch]);

  const effectiveState = useMemo(
    () => applyStateSeed(state, live ? undefined : stateSeed),
    [state, stateSeed, live],
  );

  const injectedCapabilities = useMemo<GuiCapabilities | undefined>(() => (
    seeded && capabilitiesFactory
      ? capabilitiesFactory(routedDispatch)
      : undefined
  ), [seeded, capabilitiesFactory, routedDispatch]);
  const connectedCapabilities = useMemo(
    () => backend?.status === 'connected' ? createConnectedGuiCapabilities(backend.connection) : undefined,
    [backend],
  );
  const effectiveCapabilities = useMemo<GuiCapabilities | undefined>(
    () => connectedCapabilities === undefined
      ? capabilities ?? injectedCapabilities
      : Object.freeze({ ...connectedCapabilities, ...(capabilities ?? {}) }),
    [capabilities, connectedCapabilities, injectedCapabilities],
  );
  const firstRunWizardEnabled = effectiveCapabilities?.firstRunWizardEnabled
    ?? firstRunWizardOffered(effectiveCapabilities?.firstRun);
  const gatedDispatch = useFirstRunRouteGate(firstRunWizardEnabled, routedDispatch);
  const backendState: BackendState = backend ?? { status: 'unavailable' };
  return (
    <BackendProvider value={backendState}>
    <GuiCapabilitiesProvider value={effectiveCapabilities}>
    <AppShell
      connected={connected}
      firstRunScenario={seeded ? 'isolated' : 'live'}
      live={live}
      chatContract={chatContract}
      firstRunChat={firstRunChat}
      state={effectiveState}
      dispatch={gatedDispatch}
      firstRunWizardEnabled={firstRunWizardEnabled}
      orgModel={live ? undefined : orgModel}
      orgCopy={orgCopy}
      agentSeed={live ? undefined : agentSeed}
      agentGateway={live ? undefined : agentGateway}
      machineModel={live ? undefined : machineModel}
      topologyModel={live ? undefined : topologyModel}
      startSummary={live ? undefined : startSummary}
    />
    </GuiCapabilitiesProvider>
    </BackendProvider>
  );
}
