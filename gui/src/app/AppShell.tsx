import type { ChatPageRuntime } from '../pages/live/LiveChatPage';
import type { Dispatch } from 'react';
import { LiveChatPage, LiveLarkPage, type LiveRuntime } from '../pages/live';
import type { StubOperations } from '../adapters/stub';
import type { AppAction, AppState } from '../state/types';
import type { DevModuleId } from '../dev-mode';
import type { FirstRunScenario } from '../features/onboarding';
import type { GuiPage } from '../state/gui-view-model';
import { QuickChat } from '../features/quick-chat/QuickChat';
import { copyOrgInvitation } from '../features/org/org-copy';
import type { OrgCopyHandler, OrgViewModel } from '../pages/org/org-view-model';
import type { MachineSectionModel } from '../features/network/machine-view-model';
import type { TopologyModel } from '../features/network/topology-view-model';
import type { LinkedSession, SessionAction } from '../dev-mode/session/session-types';
import type { ModelServicesSectionModel } from '../features/connections/model-services-view-model';
import type { MessageConnectionModel } from '../features/connections/message-connection-view-model';
import type { AgentSeed } from '../state/agent-selectors';
import type { AgentMutationGateway } from '../features/agents/AgentMutationGateway';
import { LogoutDialog } from '../components/LogoutDialog';
import { ToastHost } from '../components/ToastHost';
import '../components/notification.css';
import { Rail } from '../components/Rail';
import { ChatPage } from '../pages/ChatPage';
import { ConnectionsPage } from '../pages/ConnectionsPage';
import { ContactsPage } from '../pages/ContactsPage';
import { FirstRunPage } from '../pages/FirstRunPage';
import { OrgPage } from '../pages/OrgPage';
import { SettingsPage } from '../pages/SettingsPage';
import { StartPage } from '../pages/StartPage';
import { TasksPage } from '../pages/TasksPage';

interface AppShellProps {
  readonly firstRunScenario?: FirstRunScenario;
  readonly live?: LiveRuntime | null;
  readonly chatContract?: ChatPageRuntime | null;
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly stub: StubOperations;
  readonly devLabels?: Readonly<Record<GuiPage, DevModuleId>>;
  /** Deterministic organization model supplied by a composition entry. */
  readonly orgModel?: OrgViewModel;
  readonly session?: LinkedSession;
  /** Clipboard hook override; the shell copies locally by default. */
  readonly orgCopy?: OrgCopyHandler;
  /** Deterministic Agent data and gateway supplied by a composition entry. */
  readonly agentSeed?: AgentSeed;
  readonly agentGateway?: AgentMutationGateway;
  readonly machineModel?: MachineSectionModel;
  readonly topologyModel?: TopologyModel;
  readonly onSessionAction?: (action: SessionAction) => void;
  readonly modelSection?: ModelServicesSectionModel;
  readonly messageSection?: MessageConnectionModel;
  readonly chatChannel?: 'in-app' | 'feishu';
  readonly messageAgents?: readonly { readonly id: string; readonly name: string }[];
  readonly messageDefaultAgentId?: string | null;
}

function renderPage(
  state: AppState,
  dispatch: Dispatch<AppAction>,
  stub: StubOperations,
  orgModel?: OrgViewModel,
  orgCopy?: OrgCopyHandler,
  agentSeed?: AgentSeed,
  agentGateway?: AgentMutationGateway,
  machineModel?: MachineSectionModel,
  topologyModel?: TopologyModel,
  onSessionAction?: (action: SessionAction) => void,
  modelSection?: ModelServicesSectionModel,
  messageSection?: MessageConnectionModel,
  chatChannel?: 'in-app' | 'feishu',
  messageAgents?: readonly { readonly id: string; readonly name: string }[],
  messageDefaultAgentId?: string | null,
  live?: LiveRuntime | null,
  session?: LinkedSession,
  chatContract?: ChatPageRuntime | null,
) {
  switch (state.page) {
    case 'chat':
      if (chatContract) return <LiveChatPage live={chatContract} />;
      if (live) return <LiveChatPage live={live} />;
      return (
        <ChatPage
          state={state}
          dispatch={dispatch}
          message={messageSection}
          channel={chatChannel}
          onChannelChange={(channel) =>
            onSessionAction?.({ type: 'chat-channel-set', channel })
          }
        />
      );
    case 'tasks':
      return <TasksPage />;
    case 'contacts':
      return (
        <ContactsPage unavailable={!!live}
          state={state}
          dispatch={dispatch}
          stub={stub}
          seed={live ? { agents: [], connections: [], organizations: [] } : agentSeed}
          gateway={live ? undefined : agentGateway}
        />
      );
    case 'org':
      return (
        <OrgPage
          dispatch={dispatch}
          stub={stub}
          model={orgModel}
          session={session}
          machineModel={machineModel}
          topologyModel={topologyModel}
          onSessionAction={onSessionAction}
          onCopy={orgModel ? (orgCopy ?? copyOrgInvitation()) : undefined}
        />
      );
    case 'connections':
      return (
        <ConnectionsPage
          state={state}
          dispatch={dispatch}
          stub={stub}
          channelContent={live ? <LiveLarkPage live={live} /> : undefined}
          model={modelSection}
          onModelAction={onSessionAction}
          message={messageSection}
          onMessageAction={onSessionAction}
          messageAgents={messageAgents}
          messageDefaultAgentId={messageDefaultAgentId}
        />
      );
    case 'settings':
      return <SettingsPage dispatch={dispatch} stub={stub} />;
    case 'first-run':
      return <FirstRunPage state={state} dispatch={dispatch} />;
    case 'start':
    default:
      return <StartPage state={state} dispatch={dispatch} stub={stub} />;
  }
}

export function AppShell({
  firstRunScenario = 'live',
  state,
  dispatch,
  stub,
  devLabels,
  orgModel,
  session,
  orgCopy,
  live,
  chatContract,
  agentSeed,
  agentGateway,
  machineModel,
  topologyModel,
  onSessionAction,
  modelSection,
  messageSection,
  chatChannel,
  messageAgents,
  messageDefaultAgentId,
}: AppShellProps) {
  if (state.page === 'first-run' || (firstRunScenario === 'demo' && !live && state.firstRun.status !== 'complete' && state.page !== 'settings')) {
    return (
      <div className="app app-first-run">
        <FirstRunPage state={state} dispatch={dispatch} scenario={firstRunScenario} />
      </div>
    );
  }

  const contentClass =
    state.page === 'chat' || state.page === 'tasks'
      ? 'content content-wide'
      : 'content';
  return (
    <div className="app">
      <div className="window">
        <div className="shell">
          <Rail
            page={state.page}
            accountMenuOpen={state.accountMenuOpen}
            dispatch={dispatch}
            devLabels={devLabels}
            notifications={{
              open: state.notificationCenterOpen,
              unread: state.centerUnread,
              items: state.centerNotices,
            }}
          />

          <main className="main">
            {live && <p className="live-mode-notice" role="note">
              {live.status === 'ready' ? '聊天和飞书连接使用本机服务。' : live.status === 'error' ? '聊天和飞书服务尚未连接。' : '正在确认聊天和飞书服务。'}
              账号、模型和组织状态尚未确认。
            </p>}
            <div className={`${contentClass}${live ? ' live-mode-content' : ''}`}>
              {renderPage(
                state,
                dispatch,
                stub,
                orgModel,
                orgCopy,
                agentSeed,
                agentGateway,
                machineModel,
                topologyModel,
                onSessionAction,
                modelSection,
                messageSection,
                chatChannel,
                messageAgents,
                messageDefaultAgentId,
                live,
                session,
                chatContract,
              )}
            </div>
          </main>
        </div>
      </div>

      {!live && !chatContract && <QuickChat state={state} dispatch={dispatch} />}

      <ToastHost notices={state.notices} dispatch={dispatch} />

      {state.logoutDialogOpen && <LogoutDialog dispatch={dispatch} />}
    </div>
  );
}
