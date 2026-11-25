/**
 * Decision record (WP1 parity/dev composition module, 2026-11-09; deep modules
 * 2026-11-10):
- This module holds the mock composition: the scenario URL contract
  (`hyprialScenario` + `hyprialFirstRun`), the scenario id codec, the fixture
  kit and both mock entry renderers. It lives under `src/dev` because it
  statically consumes the development scenario/mock modules; it is only
  reachable through dynamic imports inside the `__HYPRIAL_DEV_PREVIEW__` and
  `__HYPRIAL_PARITY__` branches of the entry, so the formal `dist` DCEs the
  module (and every scenario string with it) while `dist-parity` keeps it.
- The `dist-parity` build registers the chat mock port exactly like the
  development entry: the wizard conversation step needs the contract runtime,
  and the parity artifact is a test product, never a release artifact — the
  formal artifact gate scans only the formal `dist`.
- The development entry consumes the preview store from `./fixture-tools`
  (fixture state, not part of the floating tool layer); a present scenario id selects the pure URL
  derivation on both entries (identical ids render identical trees), while the
  parameterless development preview keeps the linked-session workflow.
- Both entries inject the same `createFixtureState` result into the single
  product `App`; no fixture branch exists inside the product composition and
  the floating tools layer renders outside the product `#root`.
- Scenario id codec: `default` or `key=value;…` over the seven parity
  dimensions (j1/j2/j3/j5/j6/j7/orgData); j3 seeds are development-only and
  have no parity effect; j4/j10 stay session-tool dimensions.
- Linked organization projections depend on organization slices, not the
  session revision, so first-run actions do not rebuild capabilities and reset
  the shared controller.
- Dev tools seam (PR 4a, 2026-11-14): the development entry no longer renders
  the floating tools itself. It loads `./tools` through a dynamic import and
  calls `mountDevTools` with the fixture context from `./fixture-tools`; the
  same call with a backend context is how a real-backend entry will mount the
  layer. The tools are mounted before the product root is rendered so the
  layer is present by the time the product root has children. This module
  must not import `./index`, `./DevPreviewApp` or `./tools` statically.
 */
import { StrictMode, useCallback, useMemo, useRef, useState, useSyncExternalStore, type Dispatch } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '../App';
import { useChatContractRuntime, registerAgentChatMockPort } from './mock/use-contract-runtime';
import { createAgentChatMock } from './mock/agent-chat';
import { createMockSharedCredentialsApi } from './mock/agent-settings';
import type { MockAgentRow } from './mock/startup-agents';
import { createAgentJourney } from './journey';
import { createFixtureFirstRunActions, type AccountLoginScenario } from './first-run-capabilities';
import { syntheticProviderLoginFiles, type AgentProviderScenario } from './mock/agent-provider';
import { createDevSettingsApis } from './mock/settings';
import { isJ1Scenario } from './DevScenarioSelector';
import { createFixtureState } from './fixtures/fixture-state';
import { createEmptyState } from '../state/appReducer';
import { buildAgentScenario, isJ2Scenario } from './j2-scenarios';
import { buildChatSeed, chatContractScenarioForJ3, isJ3Scenario } from './chat-scenarios';
import { buildOrgScenario, isJ5Scenario } from './org-scenarios';
import { buildMachineScenario, isJ6Scenario } from './machine-scenarios';
import { buildTopologyScenario, isJ7Scenario } from './topology-scenarios';
import { buildOrgDevPreview, isOrgDevScenario, type OrgDevPreview } from './org/scenarios';
import { LINKED_ACTION_REFUSED, legacyOrgSource } from './legacy-org-source';
import { sessionReducer } from './session/session-reducer';
import {
  selectAgentSeed,
  selectChatSeed,
  selectChatTargets,
  selectConnections,
  selectFirstRunState,
  selectMachineModel,
  selectOrgModel,
  selectTopologyModel,
} from './session/session-selectors';
import { createSessionAgentGateway } from './session/linked-gateways';
import type { LinkedSession, SessionAction } from './session/session-types';
import { createFixtureDevToolsContext, getDevPreviewStore, type DevScenarioState } from './fixture-tools';
import type { GuiCapabilities } from '../app/capabilities';
import { createMockPlatformIntegrationsApi } from './mock/platform-integrations';
import type {
  AppAction,
  ChatSeed,
  FirstRunMode,
  StateSeed,
} from '../state/types';
import { buildManagedAgent, type AgentSeed } from '../state/agent-selectors';
import { createLocalAgentGateway, type AgentMutationGateway } from '../features/agents';
import type { MachineSectionModel, TopologyModel } from '../features/network';
import type { OrgViewModel } from '../pages/org/org-view-model';
import type { StartSummary } from '../pages/start/start-summary';

const SCENARIO_PARAM = 'hyprialScenario';
const FIRST_RUN_PARAM = 'hyprialFirstRun';
const DEFAULT_SCENARIO_ID = 'default';

const FIRST_RUN_MODES: readonly FirstRunMode[] = [
  'simulate',
  'unavailable',
  'live',
  'model-unavailable',
  'account-failure',
  'model-failure',
  'conversation-failure',
  'complete',
];

interface ScenarioSnapshot {
  readonly j1: string;
  readonly j2: string;
  readonly j3: string;
  readonly j5: string;
  readonly j6: string;
  readonly j7: string;
  readonly orgData: string;
}

const DEFAULT_SCENARIO: ScenarioSnapshot = {
  j1: 'simulate',
  j2: 'baseline',
  j3: 'baseline',
  j5: 'baseline',
  j6: 'baseline',
  j7: 'baseline',
  orgData: 'off',
};

type ScenarioGuard<T extends string> = (value: string) => value is T;

interface ScenarioGuards {
  isJ2Scenario: ScenarioGuard<string>;
  isJ5Scenario: ScenarioGuard<string>;
  isJ6Scenario: ScenarioGuard<string>;
  isJ7Scenario: ScenarioGuard<string>;
  isOrgDevScenario: ScenarioGuard<string>;
}

export function scenarioFromId(id: string, guards: ScenarioGuards): ScenarioSnapshot {
  if (id === 'logged-out-init') {
    return { ...DEFAULT_SCENARIO, j1: 'logged-out-init', j2: 'm05' };
  }
  if (id === 'logged-in-init') {
    return { ...DEFAULT_SCENARIO, j1: 'logged-in-init', j2: 'm05' };
  }
  if (id === DEFAULT_SCENARIO_ID || id.trim() === '') {
    return DEFAULT_SCENARIO;
  }
  const scenario = { ...DEFAULT_SCENARIO };
  for (const pair of id.split(';')) {
    const separator = pair.indexOf('=');
    if (separator <= 0) {
      continue;
    }
    const key = pair.slice(0, separator);
    const value = pair.slice(separator + 1);
    switch (key) {
      case 'j1':
        if (isJ1Scenario(value)) {
          scenario.j1 = value;
        }
        break;
      case 'j2':
        if (guards.isJ2Scenario(value)) {
          scenario.j2 = value;
        }
        break;
      case 'j3':
        scenario.j3 = value;
        break;
      case 'j5':
        if (guards.isJ5Scenario(value)) {
          scenario.j5 = value;
        }
        break;
      case 'j6':
        if (guards.isJ6Scenario(value)) {
          scenario.j6 = value;
        }
        break;
      case 'j7':
        if (guards.isJ7Scenario(value)) {
          scenario.j7 = value;
        }
        break;
      case 'orgData':
        if (guards.isOrgDevScenario(value)) {
          scenario.orgData = value;
        }
        break;
    }
  }
  if (isAuthInit(scenario) && scenario.j2 === 'baseline') scenario.j2 = 'm05';
  return scenario;
}

function isAuthInit(scenario: ScenarioSnapshot): boolean {
  return scenario.j1 === 'logged-out-init' || scenario.j1 === 'logged-in-init';
}

function isFirstRunMode(value: string): value is FirstRunMode {
  return (FIRST_RUN_MODES as readonly string[]).includes(value);
}

interface J1Inputs {
  readonly firstRunMode: FirstRunMode;
  readonly providerScenario: AgentProviderScenario | null;
  readonly accountScenario: AccountLoginScenario | null;
}

function resolveJ1(value: string): J1Inputs {
  if (value === 'logged-out-init') {
    return { firstRunMode: 'simulate', providerScenario: null, accountScenario: 'empty' };
  }
  if (value === 'logged-in-init') {
    return { firstRunMode: 'complete', providerScenario: null, accountScenario: 'ready' };
  }
  if (value.startsWith('provider-')) {
    const provider = value.slice('provider-'.length);
    return {
      firstRunMode: 'complete',
      providerScenario: (['success', 'empty', 'error', 'unconfigured'] as const).includes(provider as never)
        ? provider as AgentProviderScenario
        : null,
      accountScenario: null,
    };
  }
  if (value.startsWith('account-')) {
    const account = value.slice('account-'.length);
    return {
      firstRunMode: 'complete',
      providerScenario: null,
      accountScenario: (['success', 'empty', 'error', 'disabled', 'ready'] as const).includes(account as never)
        ? account as AccountLoginScenario
        : null,
    };
  }
  return isFirstRunMode(value)
    ? { firstRunMode: value, providerScenario: null, accountScenario: null }
    : { firstRunMode: 'simulate', providerScenario: null, accountScenario: null };
}

interface FixtureKit {
  createFixtureFirstRunActions: typeof createFixtureFirstRunActions;
  syntheticProviderLoginFiles: typeof syntheticProviderLoginFiles;
  buildAgentScenario: typeof buildAgentScenario;
  buildOrgScenario: typeof buildOrgScenario;
  buildMachineScenario: typeof buildMachineScenario;
  buildTopologyScenario: typeof buildTopologyScenario;
  buildOrgDevPreview: typeof buildOrgDevPreview;
  legacyOrgSource: typeof legacyOrgSource;
  isJ2Scenario: typeof isJ2Scenario;
  isJ5Scenario: typeof isJ5Scenario;
  isJ6Scenario: typeof isJ6Scenario;
  isJ7Scenario: typeof isJ7Scenario;
  isOrgDevScenario: typeof isOrgDevScenario;
}

const FIXTURE_KIT: FixtureKit = {
  createFixtureFirstRunActions,
  syntheticProviderLoginFiles,
  buildAgentScenario,
  buildOrgScenario,
  buildMachineScenario,
  buildTopologyScenario,
  buildOrgDevPreview,
  legacyOrgSource,
  isJ2Scenario,
  isJ5Scenario,
  isJ6Scenario,
  isJ7Scenario,
  isOrgDevScenario,
};

interface FixtureInputs {
  readonly firstRunMode: FirstRunMode;
  readonly providerScenario: AgentProviderScenario | null;
  readonly accountScenario: AccountLoginScenario | null;
  readonly agentSeed: AgentSeed | undefined;
  readonly agentGateway: AgentMutationGateway | undefined;
  readonly orgModel: OrgViewModel | undefined;
  readonly machineModel: MachineSectionModel | undefined;
  readonly topologyModel: TopologyModel | undefined;
  readonly orgPreview: OrgDevPreview | null;
}

function deriveFixtureInputs(kit: FixtureKit, scenario: ScenarioSnapshot): FixtureInputs {
  const j1 = resolveJ1(scenario.j1);
  const agent = isAuthInit(scenario)
    ? { seed: { agents: [], connections: [], organizations: [] }, gateway: createLocalAgentGateway({ agents: [], connections: [], organizations: [] }) }
    : kit.isJ2Scenario(scenario.j2) ? kit.buildAgentScenario(scenario.j2) : undefined;
  const orgModel = kit.isJ5Scenario(scenario.j5) ? kit.buildOrgScenario(scenario.j5) : undefined;
  const machineModel = kit.isJ6Scenario(scenario.j6) ? kit.buildMachineScenario(scenario.j6) : undefined;
  const orgPreview = scenario.orgData === 'off' || !kit.isOrgDevScenario(scenario.orgData)
    ? null
    : kit.buildOrgDevPreview(scenario.orgData);
  const topologyModel = kit.isJ7Scenario(scenario.j7)
    ? kit.buildTopologyScenario(scenario.j7, {
        members: orgModel?.members ?? [],
        machines: machineModel?.machines ?? [],
      })
    : undefined;
  return {
    firstRunMode: j1.firstRunMode,
    providerScenario: j1.providerScenario,
    accountScenario: j1.accountScenario,
    agentSeed: agent?.seed,
    agentGateway: agent?.gateway,
    orgModel: orgModel ?? undefined,
    machineModel,
    topologyModel,
    orgPreview,
  };
}

interface SessionKit {
  selectFirstRunState: typeof selectFirstRunState;
  selectChatTargets: typeof selectChatTargets;
  selectConnections: typeof selectConnections;
  selectChatSeed: typeof selectChatSeed;
  selectOrgModel: typeof selectOrgModel;
  selectAgentSeed: typeof selectAgentSeed;
  selectMachineModel: typeof selectMachineModel;
  selectTopologyModel: typeof selectTopologyModel;
  createSessionAgentGateway: typeof createSessionAgentGateway;
}

const SESSION_KIT: SessionKit = {
  selectFirstRunState,
  selectChatTargets,
  selectConnections,
  selectChatSeed,
  selectOrgModel,
  selectAgentSeed,
  selectMachineModel,
  selectTopologyModel,
  createSessionAgentGateway,
};

function FixtureProductRoot({
  kit,
  sessionKit,
  scenario,
  session,
  dispatchSession,
  firstRunWizardEnabled,
  chatContract,
  firstRunChat,
  chatSeed,
}: {
  readonly kit: FixtureKit;
  readonly sessionKit: SessionKit | null;
  readonly scenario: ScenarioSnapshot;
  readonly session: LinkedSession | null;
  readonly dispatchSession: ((action: SessionAction) => void) | null;
  readonly firstRunWizardEnabled: boolean;
  readonly chatContract: ReturnType<typeof useChatContractRuntime>;
  readonly firstRunChat: ReturnType<typeof useChatContractRuntime>;
  readonly chatSeed?: ChatSeed;
}) {
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const inputs = useMemo(() => deriveFixtureInputs(kit, scenario), [kit, scenario]);
  const linked = sessionKit && session !== null ? { kit: sessionKit, session } : null;
  const sessionFirstRun = linked ? linked.kit.selectFirstRunState(linked.session) : null;
  const firstRunMode = sessionFirstRun ? sessionFirstRun.mode : inputs.firstRunMode;
  const sessionGateway = useMemo(
    () => (
      linked && dispatchSession
        ? linked.kit.createSessionAgentGateway(() => sessionRef.current as LinkedSession, dispatchSession)
        : undefined
    ),
    [linked, dispatchSession],
  );

  const orgModel = useMemo(
    () => linked ? linked.kit.selectOrgModel(linked.session) : inputs.orgModel,
    [
      linked?.session.org,
      linked?.session.members,
      linked?.session.invites,
      linked?.session.activeViewer,
      linked?.session.firstRun.error,
      inputs.orgModel,
    ],
  );
  const orgSource = useMemo(() => {
    if (inputs.orgPreview?.source) return inputs.orgPreview.source;
    if (!orgModel) return undefined;
    return kit.legacyOrgSource(
      orgModel,
      dispatchSession ? (action) => dispatchSession(action) : undefined,
      dispatchSession
        ? (action) => {
            const current = sessionRef.current;
            if (!current) return undefined;
            const next = sessionReducer(current, action);
            if (next === current) return LINKED_ACTION_REFUSED;
            if (action.type === 'org-join' && next.firstRun.error) return next.firstRun.error;
            return undefined;
          }
        : undefined,
      sessionRef.current?.org?.name,
      () => sessionRef.current ?? undefined,
    );
  }, [kit, orgModel, inputs.orgPreview, dispatchSession]);

  const fixtureChatSeed = chatSeed ?? (linked ? linked.kit.selectChatSeed(linked.session) ?? undefined : undefined);
  const cleanInit = isAuthInit(scenario);
  const [initAgents, setInitAgents] = useState<readonly MockAgentRow[]>([]);
  const initAgentsRef = useRef(initAgents);
  initAgentsRef.current = initAgents;
  const journey = useMemo(() => createAgentJourney(setInitAgents), [scenario.j1]);
  const fixtureState = useMemo(
    () => cleanInit
      ? { ...createEmptyState(firstRunMode), chatSendMode: 'reply' as const, chatReplyText: '模拟回复：已收到消息；未调用真实 Agent。' }
      : createFixtureState(firstRunMode, null, fixtureChatSeed),
    [cleanInit, firstRunMode, fixtureChatSeed],
  );
  const postLogin = cleanInit || scenario.j1 === 'complete'
    || scenario.j1 === 'account-success'
    || scenario.j1 === 'provider-success';
  const agentSettings = useMemo(
    () => postLogin
      ? {
          sharedCredentials: createMockSharedCredentialsApi(cleanInit ? { entries: [], authGrants: [] } : undefined),
          platformIntegrations: createMockPlatformIntegrationsApi(cleanInit ? { integrations: [] } : undefined),
          ...(cleanInit ? {
            ...journey,
          } : {}),
        }
      : undefined,
    [cleanInit, postLogin, journey],
  );
  const startSummary = useMemo<StartSummary | undefined>(() => {
    if (cleanInit || !postLogin || !inputs.agentSeed) {
      return undefined;
    }
    const agents = inputs.agentSeed.agents.map((record) =>
      buildManagedAgent(record, inputs.agentSeed!.connections, inputs.agentSeed!.organizations),
    );
    const normalAgents = agents.filter((agent) => agent.availability === 'available').length;
    const adapterConnections = inputs.agentSeed.connections.filter((connection) => connection.kind === 'adapter');
    const boundAdapters = adapterConnections.filter((connection) => connection.usedBy > 0);
    const normalBindings = boundAdapters.filter((connection) => connection.status === 'connected').length;
    return {
      agents: { normal: normalAgents, abnormal: agents.length - normalAgents },
      platformBindings: { normal: normalBindings, abnormal: boundAdapters.length - normalBindings },
    };
  }, [cleanInit, initAgents, inputs.agentSeed, postLogin]);

  const capabilitiesFactory = useCallback((dispatch: Dispatch<AppAction>): GuiCapabilities => {
    const firstRun = kit.createFixtureFirstRunActions(firstRunMode, dispatch, inputs.accountScenario ?? undefined);
    const settingsApis = createDevSettingsApis({
      getAccountState: firstRun.getAccountState, actorCount: () => initAgentsRef.current.length,
    });
    return Object.freeze({
    firstRun,
    ...settingsApis,
    firstRunWizardEnabled: cleanInit ? false : firstRunWizardEnabled,
    ...(inputs.providerScenario
      ? {
          agentProviderLoginFiles: async () => kit.syntheticProviderLoginFiles(),
        }
      : {}),
    ...(orgSource ? { orgSource } : {}),
    ...(agentSettings ?? {}),
  });
  }, [agentSettings, cleanInit, kit, firstRunMode, firstRunWizardEnabled, inputs, orgSource]);
  const stateSeed: StateSeed | undefined = cleanInit
    ? { targets: initAgents.map(agent => ({
        id: agent.uri, kind: 'agent' as const, displayName: agent.displayName, role: 'Agent',
        availability: agent.harness === null ? 'needs-connection' as const : 'available' as const, avatarTone: 'green' as const,
        modelLabel: agent.model ?? '未设置', capabilities: [],
      })) }
    : linked
    ? {
        firstRun: sessionFirstRun ?? undefined,
        targets: linked.kit.selectChatTargets(linked.session),
        connections: linked.kit.selectConnections(linked.session),
        threads: linked.kit.selectChatSeed(linked.session)?.threads,
      }
    : inputs.agentSeed
      ? {
          connections: inputs.agentSeed.connections,
          targets: inputs.agentSeed.agents.map((record) => {
            const view = buildManagedAgent(
              record,
              inputs.agentSeed!.connections,
              inputs.agentSeed!.organizations ?? [],
            );
            return {
              id: view.id,
              kind: 'agent' as const,
              displayName: view.displayName,
              role: view.role,
              availability: view.availability === 'unknown' ? ('needs-connection' as const) : view.availability,
              avatarTone: view.avatarTone,
              modelServiceId: view.modelServiceId ?? undefined,
              modelLabel: view.modelLabel,
              capabilities: view.capabilities,
            };
          }),
        }
      : undefined;
  return (
    <App
      key={JSON.stringify(scenario)}
      initialState={fixtureState}
      capabilitiesFactory={capabilitiesFactory}
      chatContract={cleanInit ? null : chatContract}
      firstRunChat={firstRunChat}
      orgModel={orgModel}
      agentSeed={linked ? linked.kit.selectAgentSeed(linked.session) : inputs.agentSeed}
      agentGateway={linked ? sessionGateway ?? inputs.agentGateway : inputs.agentGateway}
      machineModel={linked ? linked.kit.selectMachineModel(linked.session) : inputs.machineModel}
      topologyModel={linked ? linked.kit.selectTopologyModel(linked.session) : inputs.topologyModel}
      startSummary={linked ? undefined : startSummary}
      stateSeed={stateSeed}
      onFirstRunAction={dispatchSession ? (action) => dispatchSession!(action as unknown as SessionAction) : undefined}
    />
  );
}

function readScenarioInput(): { readonly scenarioId: string | null; readonly firstRunWizardEnabled: boolean } {
  const params = new URLSearchParams(window.location.search);
  return {
    scenarioId: params.get(SCENARIO_PARAM),
    firstRunWizardEnabled: params.get(FIRST_RUN_PARAM) !== 'false',
  };
}

export async function renderParityEntry(root: HTMLElement): Promise<void> {
  const { scenarioId, firstRunWizardEnabled } = readScenarioInput();
  registerAgentChatMockPort(createAgentChatMock);
  const scenario = scenarioFromId(scenarioId ?? DEFAULT_SCENARIO_ID, FIXTURE_KIT);
  const inputs = deriveFixtureInputs(FIXTURE_KIT, scenario);
  function ParityRoot() {
    const chatContract = useChatContractRuntime(isJ3Scenario(scenario.j3) ? chatContractScenarioForJ3(scenario.j3) : undefined);
    const firstRunChat = useChatContractRuntime(inputs.firstRunMode === 'conversation-failure' ? 'failure' : 'success');
    return (
      <FixtureProductRoot
        kit={FIXTURE_KIT}
        sessionKit={null}
        scenario={scenario}
        session={null}
        dispatchSession={null}
        firstRunWizardEnabled={firstRunWizardEnabled}
        chatContract={chatContract}
        firstRunChat={firstRunChat}
        chatSeed={isJ3Scenario(scenario.j3) ? buildChatSeed(scenario.j3) : undefined}
      />
    );
  }
  createRoot(root).render(
    <StrictMode>
      <ParityRoot />
    </StrictMode>,
  );
}

export async function renderDevEntry(root: HTMLElement): Promise<void> {
  const { scenarioId, firstRunWizardEnabled } = readScenarioInput();
  const store = getDevPreviewStore();
  if (scenarioId !== null) {
    const scenario = scenarioFromId(scenarioId, FIXTURE_KIT);
    store.setScenario(scenario as Partial<DevScenarioState>);
  }
  registerAgentChatMockPort(createAgentChatMock);
  function DevRoot() {
    const storeScenario = useSyncExternalStore(store.subscribeScenario, store.getScenario);
    const storeSession = useSyncExternalStore(store.subscribeSession, store.getSession);
    const scenario = useMemo<ScenarioSnapshot>(() => ({
      j1: storeScenario.j1,
      j2: storeScenario.j2,
      j3: storeScenario.j3,
      j5: storeScenario.j5,
      j6: storeScenario.j6,
      j7: storeScenario.j7,
      orgData: storeScenario.orgData,
    }), [
      storeScenario.j1,
      storeScenario.j2,
      storeScenario.j3,
      storeScenario.j5,
      storeScenario.j6,
      storeScenario.j7,
      storeScenario.orgData,
    ]);
    const currentScenarioId = new URLSearchParams(window.location.search).get(SCENARIO_PARAM);
    const chatContract = useChatContractRuntime(isJ3Scenario(scenario.j3) ? chatContractScenarioForJ3(scenario.j3) : undefined);
    const firstRunChat = useChatContractRuntime(resolveJ1(scenario.j1).firstRunMode === 'conversation-failure' ? 'failure' : 'success');
    return (
      <FixtureProductRoot
        kit={FIXTURE_KIT}
        sessionKit={SESSION_KIT}
        scenario={scenario}
        session={currentScenarioId !== null || isAuthInit(scenario) ? null : storeSession}
        dispatchSession={currentScenarioId !== null || isAuthInit(scenario) ? null : store.dispatchSession}
        firstRunWizardEnabled={firstRunWizardEnabled}
        chatContract={chatContract}
        firstRunChat={firstRunChat}
        chatSeed={currentScenarioId !== null && isJ3Scenario(scenario.j3) ? buildChatSeed(scenario.j3) : undefined}
      />
    );
  }
  const { ensureDevToolsContainer, mountDevTools } = await import('./tools');
  mountDevTools(ensureDevToolsContainer(root), createFixtureDevToolsContext());
  createRoot(root).render(
    <StrictMode>
      <DevRoot />
    </StrictMode>,
  );
}
