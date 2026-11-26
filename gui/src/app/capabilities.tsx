/**
 * Decision record (M1 same-origin host, 2026-11-05)
- Ordinary product composition receives one already-authorized backend
  connection and uses its single GeneratedClient for agents, user settings,
  chat reads and org. No page or adapter constructs a transport or credential.
- Connected App composition derives these adapters from that connection even
  when a caller supplies no capabilities object. Explicit capabilities only
  override individual entries; they do not select another client.
- Ordinary composition exposes only the registered DeepSeek credential write
  and its conservative daemon-backed read projection. Other M2 onboarding
  actions are absent; account/setup Web fallbacks are not a product path.
- Reference and native test compositions may still inject narrow capabilities,
  but an absent provider is an absent port, never an implicit network path.
- No first-run fallback factory (2026-11-15, PR 1f/F6): a composition without
  first-run ports carries an empty frozen action set and no wizard; every wizard
  control is rendered only when its action exists.
- Device initialization (2026-11-09 settings convergence, WP1): the connected
  composition supplies createConnectedDeviceActions, so firstRun.initializeDevice performs the one GeneratedClient
  onboarding.device.ensure call. Refusal is unavailable without dispatch, an
  unknown dispatch stays unknown and never auto-retries, and a receipt is ready
  only when ok and deviceReady are both true; no local fallback fakes readiness.
- Device cards (2026-11-14): a read port is handed down only when the served
  catalog admits the read its card issues (daemon.status, autoupdate.status,
  top.snapshot, log.page; the migration records also need upgrade.steps.status).
  A card without a port is not rendered. A dispatched 404 stays an ordinary
  failed read of a rendered card.
- Agent directory (2026-11-14, verification): `agents` is supplied only when the
  injected client has the agent.list handler and the served catalog does not
  refuse agent.list. Otherwise the composition has no agent source, the shell
  hides the Agent destination, and no failure card whose retry cannot succeed
  is rendered; a list read that was dispatched and failed keeps its card.
- Native Auth reader (2026-11-15, integration of the agents and connections
  areas): `nativeAuth` is handed down when agent.provider.auth.list is callable,
  independently of `sharedCredentials` (which needs agent.secret.provider-list). The
  Agent credentials tab reads its Auth list, the existing-Auth choice and the
  existing-Auth check from it, so a catalog without the Key list keeps them.
- First-run runtime configuration (2026-11-18, P2): the connected composition
  adds `configureAgent`, composed by `createFirstRunConfigureAgent`, which
  creates the first-run Agent through the shared Agent API and applies the
  chosen Key grant or native Auth files. No second credential store, arbitrary
  endpoint, or silent takeover of an existing name. A missing grant/login port
  or a catalog admission refusal for the selected credential method refuses
  before create (one notification, no Agent created); a refusal that appears
  only after create keeps the created actor and stage for reconciliation.
- Uncertain credential dispatch (2026-11-18, P2/B4): a request failure whose
  dispatch is `unknown` after `agent.create` stays `unknown` and keeps the
  created actor and credential stage for reconciliation. AgentRequestError
  carries the same dispatch as the client failure it wraps. Only a
  `not-dispatched` method refusal becomes `unavailable`; a definite failure
  stays `failed`. Nothing is retried automatically.
- Formal first-run entry (2026-11-18, P3; S2d 2026-11-22): the connected daemon
  composition enables `firstRunWizardEnabled` from the declared durable loop
  (device initialize, Agent create/start, reply begin/bind/confirm/cancel), not
  from the retired native predicate: the P2/P3 runtime step is
  `configureAgent` and the reply step completes confirmation. The Start page
  shows the resume card whenever the status receipt is not recorded with both
  configuration and reply; the route gate and the card use that one flag.
- Phase routing: not-ready catalog entries declare supported steps without
  authorizing their invocation. Bootstrap initialize and serving reply ports
  coexist in the journey; every operation retains its MethodAccess check.
- Pi start approval (2026-11-18, P3): a blank workdir is not an approval. The
  start call uses the workspace the create receipt reports for the created
  Agent; when the receipt carries none, no `approve: true` call is made and
  the result stays `unknown` with the created actor and credential retained.
 */

import { createContext, useContext, type ReactNode } from 'react';
import { accessRefusal, methodRefusal, type MethodAccess } from '../client';
import { createAccountActions, createFirstRunReplyActions, firstRunWizardOffered, type FirstRunActions, type RuntimeConfigurationResult } from '../features/onboarding';
import { createNativeAuthClient, type AuthClient } from '../platform/auth';
import { createNativeSetupClient, type SetupClient } from '../platform/setup';
import {
  createConnectedDeviceActions,
  createDiagnosticApiFromClient,
  createSettingsReadApis,
  type DiagnosticApi,
  type LocalManagementApi,
  type LogsApi,
  type UpgradeStatusApi,
} from '../features/settings';
import {
  createProviderLoginFiles,
  createProviderLoginFilePicker,
  type ProviderLoginFilePicker,
  type ProviderLoginFile,
  type NativeAuthApi,
} from '../features/agent-provider';
import {
  createChatChannelsDaemonApi,
  createChatSendApi,
  createGroupAdminApiFromClient,
  createGroupChatReadApiFromClient,
  createGroupPostApiFromClient,
  type ChatSendApi,
  type GroupAdminApi,
  type GroupChatReadApi,
  type GroupPostApi,
  type MessageReadApi,
} from '../features/chat-channels';
import { createAgentsApi, createContractAgentGateway, type AgentDraft, type AgentsApi, type AgentMutationGateway } from '../features/agents';
import { createConnectedModelKeyActions } from '../features/model';
import { AccountSessionProvider, createConnectedAccountPort, type ConnectedAccountPort } from '../features/account';
import { createOrgLiveSource, type OrgDomainSource } from '../features/org';
import {
  createNativeAuthApiFromClient,
  createSharedCredentialsApiFromClient,
  createPlatformIntegrationsApiFromClient,
  type SharedCredentialsApi,
  type PlatformIntegrationsApi,
} from '../features/connections';
import { createAgentAdapterBindingsFromClient, type AgentAdapterBindingsApi } from '../features/agents';
import type { BackendConnection } from './backend';

/** App composition only. Pages never select a process, endpoint or credential store. */
export interface GuiCapabilities {
  readonly firstRun: FirstRunActions;
  readonly localManagement?: LocalManagementApi;
  readonly logs?: LogsApi;
  readonly diagnostic?: DiagnosticApi;
  readonly upgradeStatus?: UpgradeStatusApi;
  /**
   * Route gate for the first-run wizard, decided by the injected capabilities
   * alone: connected capabilities carry `false`, fixture and unavailable
   * capabilities carry `true`; an absent field reads as enabled.
   */
  readonly firstRunWizardEnabled?: boolean;
  /** The one connected account port (receipt-driven, GeneratedClient-only). */
  readonly account?: ConnectedAccountPort;
  readonly sharedCredentials?: SharedCredentialsApi;
  /** Per-agent login file inventory; present when agent.provider.auth.list is callable. */
  readonly nativeAuth?: NativeAuthApi;
  readonly platformIntegrations?: PlatformIntegrationsApi;
  readonly agentAdapterBindings?: AgentAdapterBindingsApi;
  readonly messageRead?: MessageReadApi;
  readonly groupChatRead?: GroupChatReadApi;
  readonly groupAdmin?: GroupAdminApi;
  readonly groupPost?: GroupPostApi;
  readonly chatSend?: ChatSendApi;
  readonly chatSession?: { readonly actor: string; readonly generation: string };
  readonly chatTargets?: (kind: 'agent') => Promise<readonly {
    readonly targetUri: string; readonly targetKind: string;
    readonly deliverable: boolean; readonly actor: string | null;
    readonly agentId?: string | null;
  }[]>;
  readonly orgSource?: OrgDomainSource;
  /** Distinguishes an authoritative host source from an isolated fixture source. */
  readonly orgSourceKind?: 'host' | 'isolated';
  readonly agentProviderLoginFiles?: (actor: string, provider: string) => Promise<readonly ProviderLoginFile[]>;
  /** 创建前的文件选择：还没有 actor，但文件名/内容校验与登录入口共用。 */
  readonly agentProviderLoginFilePicker?: ProviderLoginFilePicker;
  readonly agents?: AgentsApi;
  readonly agentDelete?: AgentMutationGateway['delete'];
  /** Composition-level action availability from the served catalog. */
  readonly methodAccess?: MethodAccess;
}

const unavailableCapabilities: GuiCapabilities = Object.freeze({
  firstRun: Object.freeze({}),
  firstRunWizardEnabled: false,
});

/** Compose the first-run Agent create + credential write only from present ports. */
export function createFirstRunConfigureAgent(
  gateway: Pick<AgentMutationGateway, 'create'>,
  allowProvider: AgentsApi['allowProvider'],
  loginNative: AgentsApi['loginNative'],
  methodAccess?: MethodAccess,
  startAgent?: AgentsApi['start'],
): (draft: AgentDraft) => Promise<RuntimeConfigurationResult> {
  return async (draft) => {
    const credential = draft.credential;
    if (credential?.kind === 'key') {
      if (allowProvider === undefined) {
        return { status: 'unavailable', refusal: { code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.secret.grant' } };
      }
      const refusal = accessRefusal(methodAccess, 'agent.secret.grant', {
        provider: credential.provider, keyId: credential.keyId,
      });
      if (refusal) return { status: 'unavailable', refusal };
    }
    if (credential?.kind === 'auth') {
      if (loginNative === undefined) {
        return { status: 'unavailable', refusal: { code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.provider.login' } };
      }
      const refusal = accessRefusal(methodAccess, 'agent.provider.login', {
        provider: credential.harness,
      });
      if (refusal) return { status: 'unavailable', refusal };
    }
    const result = await gateway.create(draft);
    if (result.kind === 'refused') {
      return { status: 'unavailable', refusal: result.refusal };
    }
    if (result.kind === 'incomplete') {
      return { status: 'incomplete', message: result.message, actorUri: result.actorUri, created: true };
    }
    if (result.kind === 'failed' || result.kind === 'unknown') {
      return { status: result.kind, message: result.message };
    }
    if (result.kind !== 'created') {
      return { status: 'unknown', message: 'Agent 创建结果无法确认。' };
    }
    if (result.actorUri === undefined) {
      return { status: 'unknown', message: 'Agent 创建未返回 actor，凭据未配置。' };
    }
    if (result.created === false) {
      return {
        status: 'failed',
        message: '同名 Agent 已存在；请换一个名称，未改动现有凭据。',
      };
    }
    if (draft.requireModelConfiguration) return { status: 'incomplete', created: true, actorUri: result.actorUri,
      message: '身份已创建，模型尚未配置。请在下方导入登录文件并保存配置，再启动。' };
    if (draft.modelConfiguration) return { status: 'incomplete', created: true, actorUri: result.actorUri,
      message: 'Agent 和模型配置已保存。请核对后显式启动；尚未验证模型响应。' };
    let applyCredential: ((actorUri: string) => Promise<unknown>) | undefined;
    if (credential?.kind === 'key') {
      const key = credential;
      const allow = allowProvider;
      if (allow) {
        applyCredential = (actorUri) => allow(actorUri, key.provider, {
          keyId: key.keyId,
          grantId: `model-${key.provider}`,
          revision: 1,
        });
      }
    } else if (credential?.kind === 'auth') {
      const auth = credential;
      const login = loginNative;
      if (login) applyCredential = (actorUri) => login(actorUri, auth.harness, auth.files);
    }
    if (applyCredential) {
      try {
        await applyCredential(result.actorUri);
      } catch (error) {
        const stage = credential?.kind === 'key'
          ? 'credential-key' as const
          : credential?.kind === 'auth' ? 'credential-auth' as const : undefined;
        const refusal = methodRefusal(error);
        if (refusal) {
          return {
            status: 'unavailable',
            refusal,
            actorUri: result.actorUri,
            created: true,
            stage,
          };
        }
        const dispatch = error !== null && typeof error === 'object'
          ? (error as { readonly dispatch?: unknown }).dispatch
          : undefined;
        if (dispatch === 'unknown') {
          return {
            status: 'unknown',
            message: 'Agent 已创建，但凭据配置结果未知；请到 Agent 详情核对，不要重复创建或授权。',
            actorUri: result.actorUri,
            created: true,
            stage,
          };
        }
        return {
          status: 'failed',
          message: 'Agent 已创建，但凭据配置未完成；请到 Agent 详情核对。',
          actorUri: result.actorUri,
          created: true,
          stage,
        };
      }
    }
    if (startAgent === undefined) {
      return {
        status: 'unavailable',
        refusal: { code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.start' },
        actorUri: result.actorUri,
        created: true,
      };
    }
    const startRefusal = accessRefusal(methodAccess, 'agent.start');
    if (startRefusal) {
      return {
        status: 'unavailable',
        refusal: startRefusal,
        actorUri: result.actorUri,
        created: true,
      };
    }
    const workspace = draft.cwd?.trim() || result.agent.activity?.homePath?.trim() || undefined;
    if (draft.preferredHarness === 'pi' && workspace === undefined) {
      return {
        status: 'unknown',
        message: 'Agent 已创建并配置凭据，但创建回执没有给出实际工作目录；未批准启动，请到 Agent 详情核对。',
        actorUri: result.actorUri,
        created: true,
      };
    }
    try {
      const started = await startAgent(result.agent.id, {
        ...(!draft.modelConfiguration ? {
          ...(draft.preferredHarness ? { harness: draft.preferredHarness } : {}),
          ...(draft.provider ? { provider: draft.provider } : {}),
          ...(draft.model ? { model: draft.model } : {}),
        } : {}),
        ...(workspace ? { cwd: workspace } : {}),
        ...(draft.preferredHarness === 'pi' ? { approve: true } : {}),
      });
      if (!started.accepted || !started.running) {
        return {
          status: 'unknown',
          message: 'Agent 已创建并配置凭据，但启动未确认；请到 Agent 详情核对后再发送确认消息。',
          actorUri: result.actorUri,
          created: true,
        };
      }
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) {
        return { status: 'unavailable', refusal, actorUri: result.actorUri, created: true };
      }
      const dispatch = error !== null && typeof error === 'object'
        ? (error as { readonly dispatch?: unknown }).dispatch
        : undefined;
      return {
        status: dispatch === 'unknown' ? 'unknown' : 'failed',
        message: dispatch === 'unknown'
          ? 'Agent 已创建并配置凭据，但启动结果未知；请到 Agent 详情核对，不要重复创建。'
          : 'Agent 已创建并配置凭据，但启动失败；请到 Agent 详情重试。',
        actorUri: result.actorUri,
        created: true,
      };
    }
    return {
      status: 'configured',
      actorUri: result.actorUri,
      created: true,
      message: '运行配置已保存并启动；真实回复在下一步确认。',
    };
  };
}

function createFirstRunSkipAction(
  client: BackendConnection['client'],
  methodAccess: MethodAccess,
): NonNullable<FirstRunActions['skipFirstRun']> {
  return async (expectedRevision) => {
    const refusal = accessRefusal(methodAccess, 'onboarding.firstRun.skip');
    if (refusal) return { status: 'unavailable', refusal, message: '当前 daemon 不能跳过首次设置。' };
    try {
      const result = await client.onboarding.firstRun.skip({
        expectedRevision,
        operationKey: globalThis.crypto?.randomUUID?.() ?? `skip-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      });
      return result.firstRun.skipped === true
        ? { status: 'skipped', firstRun: result.firstRun }
        : { status: 'unknown', message: '跳过结果未确认，请刷新状态核对。' };
    } catch (error) {
      const method = methodRefusal(error);
      return method
        ? { status: 'unavailable', refusal: method, message: '当前界面无法跳过首次设置。' }
        : { status: 'unknown', message: '跳过结果未知，请刷新状态核对。' };
    }
  };
}

export function createConnectedGuiCapabilities(connection: BackendConnection): GuiCapabilities {
  const client = connection.client;
  const methodAccess: MethodAccess = connection.methodAccess;
  const chat = createChatChannelsDaemonApi(client, methodAccess);
  const account = createConnectedAccountPort(client, methodAccess);
  const reads = createSettingsReadApis(client);
  const served = (method: string) => accessRefusal(methodAccess, method) === null;
  const declared = (method: string) => {
    const entry = connection.methods?.find(candidate => candidate.name === method);
    return entry?.state === 'not-ready' || (entry?.state === 'available' && served(method));
  };
  const p3LoopDeclared = served('onboarding.status') && [
    'onboarding.initialize',
    'onboarding.firstRun.begin',
    'onboarding.firstRun.bind',
    'onboarding.firstRun.confirm',
    'onboarding.firstRun.cancel',
    'agent.create',
    'agent.start',
  ].every(declared);
  const targetsReadable = accessRefusal(methodAccess, 'targets', { kind: 'agent' }) === null;
  const messagesReadable = ['message.query', 'conversations.list']
    .some(method => accessRefusal(methodAccess, method) === null);
  const groupReadable = served('conversations.list') && served('conversations.get');
  const groupAdminAvailable = groupReadable || served('conversations.create')
    || served('conversations.members.add') || served('conversations.members.remove');
  const allAgentMethods = createAgentsApi(client, methodAccess);
  const { modelStatus, configureModel, cancelModel, nativeTargets, ...legacyAgentMethods } = allAgentMethods;
  const modelDeclared = connection.methods?.some(entry => entry.name.startsWith('agent.model.')) === true;
  const directory = modelDeclared
    ? { ...legacyAgentMethods, modelStatus, configureModel, cancelModel, nativeTargets }
    : legacyAgentMethods;
  const agents = directory.list !== undefined && accessRefusal(methodAccess, 'agent.list') === null
    ? directory : undefined;
  const sharedCredentials = accessRefusal(methodAccess, 'agent.secret.provider-list') === null
    ? createSharedCredentialsApiFromClient(client, methodAccess)
    : undefined;
  const pickLoginFiles = createProviderLoginFilePicker();
  const agentGateway = createContractAgentGateway({ api: directory });
  const allowProvider = directory.allowProvider?.bind(directory);
  const loginNative = directory.loginNative?.bind(directory);
  const chatSend = createChatSendApi({
    send: params => client.message.send({ ...params, to: [...params.to] }),
    keyLookup: params => client.message.send.keyLookup(params),
  }, {
    access: methodAccess,
    // Access may be denied by browser policy; the adapter refuses before dispatch.
    storage: {
      getItem: name => globalThis.sessionStorage.getItem(name),
      setItem: (name, value) => globalThis.sessionStorage.setItem(name, value),
      removeItem: name => globalThis.sessionStorage.removeItem(name),
    },
  });
  const replyConfirmation = declared('onboarding.firstRun.begin')
    ? createFirstRunReplyActions(
      { onboarding: client.onboarding, message: client.message }, chatSend, methodAccess,
    )
    : undefined;
  const skipFirstRun = declared('onboarding.firstRun.skip')
    ? createFirstRunSkipAction(client, methodAccess)
    : undefined;

  const firstRun = Object.freeze({
    ...createConnectedModelKeyActions(client, methodAccess),
    ...account.actions,
    logoutAccount: async () => {
      const result = await account.actions.logoutAccount!();
      if (result.status === 'logged-out') connection.beginTransition?.('logout');
      return result;
    },
    ...(declared('onboarding.initialize') ? {
      ...createConnectedDeviceActions(client, methodAccess,
        connection.beginTransition ? () => connection.beginTransition?.('initialize') : undefined),
    } : {}),
    ...(agentGateway ? {
      modelConfigurationApi: directory,
      configureAgent: createFirstRunConfigureAgent(
        agentGateway, allowProvider, loginNative, methodAccess, directory.start?.bind(directory),
      ),
    } : {}),
    ...(sharedCredentials ? { runtimeCredentials: sharedCredentials } : {}),
    ...(pickLoginFiles ? { pickRuntimeLoginFiles: pickLoginFiles } : {}),
    ...(replyConfirmation ? { replyConfirmation } : {}),
    ...(skipFirstRun ? { skipFirstRun } : {}),
  });

  return Object.freeze({
    firstRun,
    account,
    firstRunWizardEnabled: connection.descriptor.driver === 'daemon'
      && p3LoopDeclared
      && typeof firstRun.initializeDevice === 'function'
      && typeof firstRun.configureAgent === 'function'
      && replyConfirmation !== undefined,
    methodAccess,
    ...(agents === undefined ? {} : { agents }),
    ...(served('daemon.status') ? { localManagement: reads.localManagement } : {}),
    ...(served('autoupdate.status') ? {
      upgradeStatus: served('upgrade.steps.status') ? reads.upgradeStatus : { status: reads.upgradeStatus.status },
    } : {}),
    ...(served('log.page') ? { logs: reads.logs } : {}),
    ...(served('top.snapshot') ? { diagnostic: createDiagnosticApiFromClient(client) } : {}),
    ...(sharedCredentials ? { sharedCredentials } : {}),
    ...(served('agent.provider.auth.list') ? { nativeAuth: createNativeAuthApiFromClient(client, methodAccess) } : {}),
    ...(accessRefusal(methodAccess, 'adapter.list') === null
      ? { platformIntegrations: createPlatformIntegrationsApiFromClient({ adapter: client.adapter, management: client.management }, methodAccess) } : {}),
    agentAdapterBindings: createAgentAdapterBindingsFromClient(client, methodAccess),
    agentProviderLoginFiles: createProviderLoginFiles(),
    agentProviderLoginFilePicker: pickLoginFiles,
    ...(messagesReadable ? { messageRead: chat.messageRead } : {}),
    ...(groupReadable ? { groupChatRead: createGroupChatReadApiFromClient(client, methodAccess) } : {}),
    ...(groupAdminAvailable ? { groupAdmin: createGroupAdminApiFromClient(client, methodAccess) } : {}),
    groupPost: createGroupPostApiFromClient(client, { access: methodAccess }),
    chatSend,
    ...(targetsReadable
      ? { chatTargets: async (kind: 'agent') => (await client.targets({ kind })).targets } : {}),
    ...(connection.descriptor.principalActor ? { chatSession: Object.freeze({
      actor: connection.descriptor.principalActor,
      generation: connection.descriptor.generation,
    }) } : {}),
    ...(accessRefusal(methodAccess, 'org.list') === null
      ? { orgSource: createOrgLiveSource(client, methodAccess), orgSourceKind: 'host' as const }
      : {}),
  });
}

export function createNativeGuiCapabilities(account: AuthClient, setup?: SetupClient): GuiCapabilities {
  const firstRun = createAccountActions(
    createNativeAuthClient(account),
    setup ? createNativeSetupClient(setup) : undefined,
  );
  return { firstRun, firstRunWizardEnabled: firstRunWizardOffered(firstRun) };
}

export function createWebGuiCapabilities(): GuiCapabilities {
  return unavailableCapabilities;
}

export function createGuiCapabilities(account?: AuthClient, setup?: SetupClient): GuiCapabilities {
  return account ? createNativeGuiCapabilities(account, setup) : unavailableCapabilities;
}

const Capabilities = createContext<GuiCapabilities>(unavailableCapabilities);
export function GuiCapabilitiesProvider({ value, children }: {
  readonly value?: GuiCapabilities;
  readonly children: ReactNode;
}) {
  const effective = value ?? unavailableCapabilities;
  return <Capabilities.Provider value={effective}>
    <AccountSessionProvider actions={effective.firstRun}>{children}</AccountSessionProvider>
  </Capabilities.Provider>;
}
export function useGuiCapabilities(): GuiCapabilities {
  return useContext(Capabilities);
}
