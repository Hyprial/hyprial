/**
 * Decision record (WP1 product composition, 2026-11-09; deep modules 2026-11-10):
- Connected entry reads account/device/receipt status once per action source
  and account identity before routing. Only an authenticated, ready device
  with both durable confirmations bypasses setup. Explicit exits remain usable;
  a new session is evaluated again. No local completion substitutes for status.
- The shared shell mounts the notifications provider and the toast host in
  both frames (first-run and regular), so a feature reports an unavailable
  function the same way in every entry (components/notifications).
- Navigation carries only product destinations; the product rail is identical
  in every entry.
- A destination whose source or port is absent from the composition has no
  nav entry and its route renders the start page (hiddenPages, from
  capabilities and data only; never build mode or a dev token). A feature-area
  PR hides its destination by adding one line to the set in AppShell(). The
  optional `hiddenPages` prop only lets a composition or test add pages.
- The notification center is not gated by sign-in: an operation a signed-out
  user can perform (login) reports there like any other. The gate that hid it
  (account lifecycle commit 7f4f0a117) protected against entries of another
  session; that is now guaranteed at the source: leaving the authenticated
  state clears the center, so nothing from a previous session survives.
- The Agent destination exists when the composition has an agent source: a
  directory api (capabilities.agents), or, outside a live runtime, an injected
  seed or gateway. In-page navigation does not consult hiddenPages; controls
  elsewhere that open the Agent page must be rendered from the same fact.
- The one place a page action enters the Agent create panel is the shell
  dispatch: open-agent-create is checked against the served catalog
  (agent.create) before it reaches the reducer. A refusal is one
  notification and neither opens the form nor navigates, so every caller
  (Start, organization, Agent page) behaves the same.
- The org source can turn unwired at run time (its first read was refused
  before dispatch); a probe inside the provider reports that to the shell so
  the 组织 entry and route disappear instead of waiting forever; an unwired
  snapshot that is still the shared UNWIRED_SNAPSHOT is the signed-out
  leftover, not a refusal.
- The Agent 设置 destination exists when the composition hands down a shared
  credentials port or a platform integrations port (the two sections the page
  mounts). Without either the page would be a title only, so the route and
  the account-menu entry are hidden like any other absent destination.
 */
import type { ChatPageRuntime } from '../pages/live/LiveChatPage';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch } from 'react';
import { LiveChatPage, type LiveRuntime } from '../pages/live';
import type { GuiPage } from '../state/gui-view-model';
import type { AppAction, AppState } from '../state/types';
import { firstRunWizardOffered, type FirstRunScenario } from '../features/onboarding';
import {
  OrgDomainProvider,
  UNWIRED_SNAPSHOT,
  UNWIRED_SOURCE,
  copyOrgInvitation,
  useOrgDomain,
} from '../features/org';
import type { OrgCopyHandler, OrgViewModel } from '../pages/org/org-view-model';
import type { MachineSectionModel, TopologyModel } from '../features/network';
import type { AgentSeed } from '../state/agent-selectors';
import type { AgentMutationGateway } from '../features/agents';
import { useAccountSettings } from '../features/account';
import { LoginRequiredPage } from './LoginRequiredPage';
import { ToastHost } from '../components/ToastHost';
import { LoadingState } from '../components/primitives';
import { NotificationsProvider, unavailableNotice } from '../components/notifications';
import { accessRefusal } from '../client';
import '../components/notification.css';
import { Rail } from '../components/Rail';
import { ChatPage } from '../pages/ChatPage';
import { ConnectedChatPage } from '../pages/ConnectedChatPage';
import { ConnectionsPage } from '../pages/ConnectionsPage';
import { ContactsPage } from '../pages/ContactsPage';
import { FirstRunPage } from '../pages/FirstRunPage';
import { OrgPage } from '../pages/OrgPage';
import { DevicePage } from '../pages/DevicePage';
import { useGuiCapabilities } from './capabilities';
import { PageAvailabilityProvider } from './page-availability';
import { SettingsPage } from '../pages/SettingsPage';
import { StartPage } from '../pages/StartPage';
import type { StartSummary } from '../pages/start/start-summary';

interface AppShellProps {
  readonly connected?: boolean;
  readonly firstRunScenario?: FirstRunScenario;
  /** A7: route-level switch; false blocks every path into the wizard. */
  readonly firstRunWizardEnabled?: boolean;
  readonly live?: LiveRuntime | null;
  readonly chatContract?: ChatPageRuntime | null;
  /** First-run-only contract runtime; ignored when the chat page runtime exists. */
  readonly firstRunChat?: ChatPageRuntime | null;
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  /** Deterministic organization model supplied by a composition entry. */
  readonly orgModel?: OrgViewModel;
  /** Clipboard hook override; the shell copies locally by default. */
  readonly orgCopy?: OrgCopyHandler;
  /** Deterministic Agent data and gateway supplied by a composition entry. */
  readonly agentSeed?: AgentSeed;
  readonly agentGateway?: AgentMutationGateway;
  readonly machineModel?: MachineSectionModel;
  readonly topologyModel?: TopologyModel;
  readonly startSummary?: StartSummary;
  readonly hiddenPages?: ReadonlySet<GuiPage>;
}

function renderPage(
  state: AppState,
  dispatch: Dispatch<AppAction>,
  connected: boolean,
  orgModel?: OrgViewModel,
  orgCopy?: OrgCopyHandler,
  agentSeed?: AgentSeed,
  agentGateway?: AgentMutationGateway,
  machineModel?: MachineSectionModel,
  topologyModel?: TopologyModel,
  live?: LiveRuntime | null,
  chatContract?: ChatPageRuntime | null,
  firstRunChat?: ChatPageRuntime | null,
  wizardEnabled: boolean = true,
  startSummary?: StartSummary,
  hiddenPages: ReadonlySet<GuiPage> = new Set(),
  firstRunPending = false,
  onResumeFirstRun?: () => void,
) {
  const manageTarget = hiddenPages.has('contacts') ? undefined
    : (agentId: string) => dispatch({ type: 'open-agent', agentId });
  switch (state.page) {
    case 'chat':
      if (connected) return <ConnectedChatPage initialTargetId={state.chatIntentTargetId}
        openedTargetIds={state.openedChatTargetIds}
        onIntentConsumed={() => dispatch({ type: 'clear-chat-intent' })} />;
      if (chatContract) return <LiveChatPage live={chatContract} onManageTarget={manageTarget} />;
      if (live) return <LiveChatPage live={live} onManageTarget={manageTarget} />;
      return (
        <ChatPage
          state={state}
          dispatch={dispatch}
        />
      );
    case 'contacts':
      return (
        <ContactsPage
          state={state}
          dispatch={dispatch}
          seed={live ? undefined : agentSeed}
          gateway={live ? undefined : agentGateway}
        />
      );
    case 'org':
      return (
        <OrgPage
          dispatch={dispatch}
          model={orgModel}
          machineModel={machineModel}
          topologyModel={topologyModel}
          onCopy={orgModel ? (orgCopy ?? copyOrgInvitation()) : undefined}
        />
      );
    case 'device':
      return <DevicePage />;
    case 'connections':
      return <ConnectionsPage />;
    case 'settings':
      return <SettingsPage />;
    case 'first-run':
      // Hidden, not deleted: the component tree and its step semantics stay.
      return wizardEnabled
        ? <FirstRunPage dispatch={dispatch} chat={chatContract ?? firstRunChat ?? live} />
        : <StartPage state={state} dispatch={dispatch} summary={startSummary} />;
    case 'start':
    default:
      return <StartPage state={state} dispatch={dispatch} summary={startSummary}
        firstRunPending={firstRunPending} onResumeFirstRun={onResumeFirstRun} />;
  }
}

export function AppShell({
  connected = false,
  firstRunScenario = 'live',
  firstRunWizardEnabled,
  state: committedState,
  orgModel,
  orgCopy,
  live,
  chatContract,
  firstRunChat,
  agentSeed,
  agentGateway,
  machineModel,
  topologyModel,
  startSummary,
  hiddenPages: composedHiddenPages,
  dispatch: hostDispatch,
}: AppShellProps) {
  const domain = useOrgDomain();
  const capabilities = useGuiCapabilities();
  const methodAccess = capabilities.methodAccess;
  const dispatch = useCallback<Dispatch<AppAction>>((action) => {
    if (action.type === 'open-agent-create') {
      const refusal = accessRefusal(methodAccess, 'agent.create');
      if (refusal) {
        hostDispatch({ type: 'push-notice', ...unavailableNotice('创建 Agent', refusal) });
        return;
      }
    }
    hostDispatch(action);
  }, [hostDispatch, methodAccess]);
  const agentsAvailable = capabilities.agents?.list !== undefined
    || (!live && (agentSeed !== undefined || agentGateway !== undefined));
  const account = useAccountSettings(capabilities.firstRun, input => dispatch({ type: 'push-notice', ...input }));
  const [orgUnwired, setOrgUnwired] = useState(false);
  const statusRefused = account.snapshot.account.status === 'unavailable' && Boolean(account.snapshot.account.refusal);
  const accountStatus = account.snapshot.account;
  const authenticated = accountStatus.status === 'authenticated';
  const orgAvailable = domain.provided
    ? domain.snapshot.status !== 'unwired'
    : capabilities.orgSource !== undefined &&
      capabilities.orgSource.initial.status !== 'unwired' &&
      !(authenticated && orgUnwired);
  const deviceHasCards = Boolean(capabilities.localManagement || capabilities.upgradeStatus || capabilities.diagnostic || capabilities.logs);
  const deviceRecoveryVisible = authenticated
    && account.snapshot.device.status === 'not-ready'
    && Boolean(account.snapshot.device.message);
  const hiddenPages = new Set<GuiPage>([
    ...(orgAvailable ? [] : ['org' as const]),
    ...(composedHiddenPages ?? []),
    ...(agentsAvailable ? [] : ['contacts' as const]),
    ...(capabilities.sharedCredentials || capabilities.platformIntegrations ? [] : ['connections' as const]),
    ...(!deviceHasCards && !deviceRecoveryVisible && (statusRefused || !account.hasDeviceInit || (authenticated && account.snapshot.device.status === 'ready'))
      || !account.hasDeviceInit && accountStatus.status === 'unauthenticated'
      ? ['device' as const] : []),
    ...(connected && !(capabilities.chatTargets && capabilities.messageRead) ? ['chat' as const] : []),
  ]);
  hiddenPages.delete('start');
  const redirected = hiddenPages.has(committedState.page);
  const state = useMemo<AppState>(
    () => (redirected ? { ...committedState, page: 'start' } : committedState),
    [committedState, redirected],
  );
  const sessionIdentity = accountStatus.status === 'authenticated' ? accountStatus.owner ?? ''
    : accountStatus.status === 'unauthenticated' || account.logoutState?.status === 'logged-out' ? null : undefined;
  useLayoutEffect(() => {
    if (sessionIdentity !== undefined) dispatch({ type: 'session-identity', identity: sessionIdentity });
  }, [sessionIdentity, dispatch]);
  const routeDispatch: Dispatch<AppAction> = action => dispatch(action.type === 'request-logout'
    ? { type: 'navigate', page: 'settings' } : action);
  const orgSource = capabilities.orgSource ?? UNWIRED_SOURCE;

  const wizardEnabled = firstRunWizardEnabled ?? capabilities.firstRunWizardEnabled
    ?? firstRunWizardOffered(capabilities.firstRun);
  const firstRunReceipt = account.snapshot.firstRun;
  const firstRunReceiptKnown = firstRunReceipt !== undefined
    && firstRunReceipt.state !== 'unavailable';
  const firstRunRecorded = firstRunReceipt?.state === 'recorded'
    && firstRunReceipt.confirmed.includes('configuration')
    && firstRunReceipt.confirmed.includes('reply');
  const firstRunSkipped = firstRunReceipt?.state === 'recorded' && firstRunReceipt.skipped === true;
  const firstRunComplete = connected
    ? authenticated && account.snapshot.device.status === 'ready' && (firstRunRecorded || firstRunSkipped)
    : firstRunReceiptKnown ? (firstRunRecorded || firstRunSkipped) : state.firstRun.status === 'complete';
  const entryDecision = useRef<{ actions: typeof capabilities.firstRun; identity: typeof sessionIdentity } | null>(null);
  useLayoutEffect(() => {
    if (!connected || !wizardEnabled || !account.statusLoaded) return;
    if (entryDecision.current?.actions === capabilities.firstRun
      && entryDecision.current?.identity === sessionIdentity) return;
    entryDecision.current = { actions: capabilities.firstRun, identity: sessionIdentity };
    if (!firstRunComplete) dispatch({ type: 'navigate', page: 'first-run' });
    else if (state.page === 'first-run') dispatch({ type: 'navigate', page: 'start' });
  }, [connected, wizardEnabled, account.statusLoaded, capabilities.firstRun, sessionIdentity, firstRunComplete, state.page, dispatch]);
  const accountKnown = account.snapshot.account.status === 'authenticated'
    || account.snapshot.account.status === 'unauthenticated';
  const firstRunPending = wizardEnabled && !firstRunComplete
    && (firstRunReceiptKnown || accountKnown);
  const firstRun =
    wizardEnabled && (state.page === 'first-run' ||
    (firstRunScenario === 'isolated' &&
      !live &&
      state.firstRun.status !== 'complete' &&
      state.page !== 'settings'));
  const wasFirstRun = useRef(firstRun);
  useEffect(() => {
    const completed = wasFirstRun.current && !firstRun;
    wasFirstRun.current = firstRun;
    if (completed) void account.run('read');
  }, [firstRun]);
  const contentClass =
    state.page === 'chat'
      ? 'content content-wide'
      : 'content';
  const toasts = <ToastHost notices={state.notices} dispatch={dispatch} />;
  const content = connected && wizardEnabled && !account.statusLoaded ? (
    <main className="app app-first-run"><LoadingState title="正在读取首次设置状态" /></main>
  ) : firstRun ? (
    <div className="app app-first-run">
      <main className="content" id="main-content">
        <FirstRunPage dispatch={dispatch} scenario={firstRunScenario} resumeFromStatus={connected} chat={chatContract ?? firstRunChat ?? live} />
      </main>
      {toasts}
    </div>
  ) : (
    <div className="app">
      <div className="window">
        <a className="skip-link" href="#main-content">跳到主要内容</a>
        <div className="shell">
          <Rail
            page={state.page}
            hiddenPages={hiddenPages}
            accountMenuOpen={state.accountMenuOpen}
            authenticated={authenticated}
            dispatch={routeDispatch}
            notifications={{
              open: state.notificationCenterOpen,
              unread: state.centerUnread,
              items: state.centerNotices,
            }}
          />

          <main className="main" id="main-content" tabIndex={-1}>
            {live && <p className="live-mode-notice" role="note">
              {live.status === 'ready' ? '聊天和飞书已连接。' : live.status === 'error' ? '聊天和飞书未连接。' : '正在连接聊天和飞书…'}
            </p>}
            <div className={`${contentClass}${live ? ' live-mode-content' : ''}`}>
              {!authenticated && state.page === 'start'
                ? <StartPage state={state} dispatch={dispatch}
                    firstRunPending={firstRunPending}
                    onResumeFirstRun={() => dispatch({ type: 'navigate', page: 'first-run' })}
                    onLoginRequired={() => dispatch({ type: 'set-notice', text: '请先登录' })} />
                : !authenticated && !['settings', 'device', 'first-run'].includes(state.page)
                ? <LoginRequiredPage key={state.page} page={state.page}
                    confirmed={account.snapshot.account.status === 'unauthenticated'}
                    onLogin={() => dispatch({ type: 'navigate', page: 'settings' })}
                    onRefresh={() => void account.run('read')} />
                : renderPage(
                state,
                dispatch,
                connected,
                orgModel,
                orgCopy,
                agentSeed,
                agentGateway,
                machineModel,
                topologyModel,
                live,
                chatContract,
                firstRunChat,
                wizardEnabled,
                startSummary,
                hiddenPages,
                firstRunPending,
                () => dispatch({ type: 'navigate', page: 'first-run' }),
              )}
            </div>
          </main>
        </div>
      </div>

      {toasts}

    </div>
  );
  return (
    <NotificationsProvider dispatch={dispatch}>
      <PageAvailabilityProvider hiddenPages={hiddenPages}>
        {domain.provided ? (
          content
        ) : (
          <OrgDomainProvider source={authenticated ? orgSource : UNWIRED_SOURCE}
            externalSnapshot={authenticated ? orgSource.initial : UNWIRED_SOURCE.initial}>
            {authenticated ? <OrgUnwiredProbe onChange={setOrgUnwired} /> : null}
            {content}
          </OrgDomainProvider>
        )}
      </PageAvailabilityProvider>
    </NotificationsProvider>
  );
}

function OrgUnwiredProbe({ onChange }: { readonly onChange: (unwired: boolean) => void }) {
  const { snapshot } = useOrgDomain();
  const unwired = snapshot.status === 'unwired' && snapshot !== UNWIRED_SNAPSHOT;
  useEffect(() => {
    onChange(unwired);
  }, [unwired, onChange]);
  return null;
}

/**
 * A7 route gate: with the first-run wizard disabled, the `navigate` action
 * into the wizard is refused at the router level — no page, side effect or
 * history entry is created. Shared by the formal and development composition
 * roots so both refuse identically.
 */
export function useFirstRunRouteGate(
  wizardEnabled: boolean,
  dispatch: Dispatch<AppAction>,
): Dispatch<AppAction> {
  return useCallback(
    (action) => {
      if (!wizardEnabled && action.type === 'navigate' && action.page === 'first-run') {
        return;
      }
      dispatch(action);
    },
    [dispatch, wizardEnabled],
  );
}
