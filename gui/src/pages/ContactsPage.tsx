// GUI live A (2026-11-04): injected directory data wins; absent capability never selects state fixtures.
// ContactsPage 读取侧接线决策（implG1，2026-11-02；round 2 按 revG1 修订）：
// - capabilities 提供 agents api 时整页由读取侧驱动：agent.list（列表）、
//   agent.home-census / agent.secret.list（详情）、adapter.list
//   （平台绑定页签）；未提供时保持原种子/占位路径；同一展示组件两条数据路径。
// - 2026-11-22（G1）：移除对 link.list 的调用及 linksApi 注入。
// - revG1 G1-1：授权记录不证明凭据可用；读取侧不把授权记录映射为「已连接/可用」，
//   开始对话不因授权记录存在而启用。
// - implG2（2026-11-02）：目录模式接入写侧——创建经契约网关（agent.create，
//   retry=never），成功/结果未知后重读列表（列表权威是 agent.list）；凭据写入经
//   agent.secret.grant/login；启动/停止经 createRuntimeActionHandler；改名经
//   agent.rename；正式删除待 HTTP 暴露，隔离组合可注入删除能力。
// - 2026-11-13：创建不自动启动；凭据写入回执与运行验证分开，成功后进入配置 Agent。
// - 2026-11-14：没有 Agent 来源（目录、种子或网关）的组合由 AppShell 隐藏本页；
//   本页不再有「暂不可用」页。没有写端口的操作不渲染（无网关 → 无创建，网关无
//   update/delete → 无编辑/删除，无 agentDelete → 无删除）；有端口但调用被目录
//   拒绝时控件保持可用，用户操作时发出通知（创建/启动/停止/改名/删除/保存凭据）。
// - 2026-11-14（验证修订）：发出前就知道被拒绝的读取不是失败，也不是空数据：端口在
//   交出处按方法目录拦截——agent.list 被拒绝则没有 agents 能力（整页由 AppShell 隐藏）；
//   agent.secret.provider-list / agent.secret.grant 被拒绝则创建表单与凭据页签没有 Key 来源，
//   agent.provider.login 被拒绝则没有 Auth 来源，两者都没有则不渲染凭据字段；
//   agent.provider.auth.list 被拒绝则没有 Auth 清单。
//   injected client 缺 handler 的 api 成员不存在，对应控件不渲染。重命名/启动/停止/
//   保存凭据在打开编辑器、对话框或开始忙碌之前先查方法目录（checkAccess），被拒绝只发
//   一条通知；创建后凭据写入被拒绝不再发通知，只陈述「凭据未配置」。
// - 2026-11-15（合并 Agent 与连接两个范围）：Auth 清单读取端口取自 capabilities.nativeAuth，只看
//   agent.provider.auth.list 是否可调用，不再依赖 sharedCredentials（后者要求 agent.secret.provider-list）。
//   agent.secret.provider-list 被拒绝时凭据页签仍有 Auth 清单、「使用已有 Auth」与已有 Auth 检查，且没有 Key 来源。
import { useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch } from 'react';
import { useGuiCapabilities } from '../app/capabilities';
import { usePageAvailable } from '../app/page-availability';
import { accessRefusal, methodRefusal } from '../client';
import { useNotifyUnavailable } from '../components/notifications';
import {
  AGENT_NOT_FOUND,
  filterManagedAgents,
  getManagedAgent,
  selectManagedAgents,
  type AgentSeed,
  type ManagedAgentView,
} from '../state/agent-selectors';
import type { AppAction, AppState } from '../state/types';
import {
  AgentDetail,
  AgentForm,
  AgentList,
  createContractAgentGateway,
  createRuntimeActionHandler,
  directoryRowToExtension,
  directoryRowToView,
  mergeOrganizationAgents,
  getAgentExtension,
  useAgentDirectory,
  type AgentDraft,
  type AgentCredentialPlan,
  type AgentAdapterBindingsApi,
  agentFailureMessage,
  toAgentRequestError,
  type AgentExtensionSeed,
  type AgentExtensionView,
  type AgentMutationGateway,
  type AgentMutationResult,
} from '../features/agents';
import { AgentOrgPanel, useOrgDomain } from '../features/org';
import { isCanonicalAgentUri } from '../features/chat-channels';
import { Button, Card } from '../components/primitives';

interface ContactsPageProps {
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly seed?: AgentSeed;
  readonly gateway?: AgentMutationGateway;
  /**
   * dev-02 private dimensions (runtime, provider grants, preset marker).
   * Absent in the product build: every dimension then renders as unknown.
   */
  readonly extensions?: AgentExtensionSeed;
  /** Fixture-only 平台绑定 API; live composition uses the connected capability. */
  readonly adapterBindingsApi?: AgentAdapterBindingsApi;
}

const LIST_PANEL: AppState['agentPanel'] = { kind: 'list' };

export function ContactsPage({
  state,
  dispatch,
  seed,
  gateway,
  extensions,
  adapterBindingsApi,
}: ContactsPageProps) {
  const capabilities = useGuiCapabilities();
  const chatAvailable = usePageAvailable('chat');
  const directoryApi = capabilities.agents?.list !== undefined ? capabilities.agents : undefined;
  const credentialsApi = capabilities.sharedCredentials;
  const nativeLoginFiles = capabilities.agentProviderLoginFiles;
  const loginFilePicker = capabilities.agentProviderLoginFilePicker;
  const methodAccess = capabilities.methodAccess;
  const adapterBindingsSource = adapterBindingsApi ?? capabilities.agentAdapterBindings;
  const adapterBindings = adapterBindingsSource !== undefined
    && accessRefusal(methodAccess, 'adapter.list') === null
    ? adapterBindingsSource
    : undefined;
  const notifyUnavailable = useNotifyUnavailable();
  const callable = (method: string): boolean => accessRefusal(methodAccess, method) === null;
  const createKeyCredentials = credentialsApi !== undefined
    && (directoryApi?.configureModel !== undefined || directoryApi?.allowProvider !== undefined)
    && callable('agent.secret.provider-list') && (directoryApi?.configureModel !== undefined || callable('agent.secret.grant'))
    ? credentialsApi : undefined;
  const createLoginFilePicker = directoryApi?.loginNative !== undefined
    && callable('agent.provider.login') ? loginFilePicker : undefined;
  const nativeAuthApi = accessRefusal(methodAccess, 'agent.provider.auth.list') === null
    ? capabilities.nativeAuth ?? credentialsApi?.nativeAuth : undefined;
  const tabCredentials = useMemo(() => credentialsApi === undefined ? undefined : {
    ...credentialsApi,
    nativeAuth: nativeAuthApi,
  }, [credentialsApi, nativeAuthApi]);
  const directoryMode = directoryApi !== undefined;
  const previewMode = seed !== undefined || gateway !== undefined;
  const directory = useAgentDirectory(directoryApi);
  const [remoteChatTargets, setRemoteChatTargets] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!chatAvailable || !capabilities.chatTargets) {
      setRemoteChatTargets({});
      return;
    }
    let active = true;
    void capabilities.chatTargets('agent').then((rows) => {
      if (!active) return;
      setRemoteChatTargets(Object.fromEntries(rows
        .filter((row) => row.deliverable && isCanonicalAgentUri(row.targetUri))
        .map((row) => [row.agentId ?? row.targetUri.split(':').at(-1)!, row.targetUri])));
    }).catch(() => { if (active) setRemoteChatTargets({}); });
    return () => { active = false; };
  }, [capabilities.chatTargets, chatAvailable]);
  const { snapshot: orgSnapshot } = useOrgDomain();
  const canConfirmMissingAgent = directoryMode ? directory.status === 'ready' : previewMode;

  const baseAgents = useMemo(
    () => previewMode ? selectManagedAgents(state, seed) : [],
    [previewMode, state, seed],
  );
  const [localAgents, setLocalAgents] = useState<
    readonly ManagedAgentView[] | null
  >(null);
  const directoryRows = directory.rows;
  const directoryViews = useMemo(
    () => directoryRows.map(directoryRowToView),
    [directoryRows],
  );
  const directoryExtensions = useMemo(
    () =>
      Object.fromEntries(
        directoryRows.map((row) => [row.actor, directoryRowToExtension(row)]),
      ) as Record<string, AgentExtensionView>,
    [directoryRows],
  );

  const agents = useMemo(
    () => directoryMode
      ? orgSnapshot.orgAgentDirectory !== undefined
        ? mergeOrganizationAgents(directoryViews, orgSnapshot.orgAgentDirectory)
        : directoryViews
      : previewMode ? (localAgents ?? baseAgents) : [],
    [directoryMode, directoryViews, orgSnapshot.orgAgentDirectory, previewMode, localAgents, baseAgents],
  );
  const gatewayClient = useMemo<AgentMutationGateway | undefined>(
    () => directoryApi
      ? createContractAgentGateway({ api: directoryApi, afterCreate: directory.reload })
      : previewMode ? gateway : undefined,
    [directoryApi, directory.reload, previewMode, gateway],
  );
  const panel = state.agentPanel.kind === 'create' && gatewayClient === undefined
    ? LIST_PANEL
    : state.agentPanel;
  const runtimeActionHandler = directoryApi
    ? createRuntimeActionHandler(directoryApi, directory.reload)
    : undefined;
  const renameAgent = directoryApi?.rename?.bind(directoryApi);
  const [pending, setPending] = useState(false);
  const [formMessage, setFormMessage] = useState<
    { tone: 'error' | 'status'; text: string } | null
  >(null);
  type CreatedNotice = {
    readonly tone: 'status' | 'error';
    readonly text: string;
    readonly agentId?: string;
  };
  const [createdNotice, setCreatedNotice] = useState<CreatedNotice | null>(null);
  const [createUnknown, setCreateUnknown] = useState(false);
  /** Page-local extension overrides for agents created in this session. */
  const [localExtensions, setLocalExtensions] = useState<
    Record<string, Partial<AgentExtensionView>>
  >({});
  const mergedExtensions: Record<string, Partial<AgentExtensionView>> = {
    ...extensions,
    ...localExtensions,
  };
  const listHeadingRef = useRef<HTMLHeadingElement>(null);
  const notFoundTitleRef = useRef<HTMLHeadingElement>(null);
  const [focusListWhenMounted, setFocusListWhenMounted] = useState(false);
  // 所有者/主机筛选是表现层维度：留在页面本地，不入全局状态。
  const [ownerFilter, setOwnerFilter] = useState('');
  const [hostFilter, setHostFilter] = useState('');

  const organizations = !directoryMode && previewMode ? seed?.organizations ?? [] : [];
  // 选项取自全量数据（而非当前过滤结果），避免选中后被自己的过滤器困住。
  const ownerOptions = [...new Set(agents.map((agent) => agent.ownerLabel))];
  const hostOptions = [...new Set(agents.map((agent) => agent.host))];
  const afterDimensions = agents.filter(
    (agent) =>
      (ownerFilter === '' || agent.ownerLabel === ownerFilter) &&
      (hostFilter === '' || agent.host === hostFilter),
  );
  const visibleAgents = filterManagedAgents(
    afterDimensions,
    state.agentQuery,
    state.agentFilter,
  );

  const selectedAgent =
    panel.kind === 'detail' ? getManagedAgent(agents, directoryRows.find(row => row.uri === panel.agentId)?.actor ?? panel.agentId) : undefined;
  const detailAgent = selectedAgent?.remote ? undefined : selectedAgent;
  const detailDirectoryRow =
    directoryMode && panel.kind === 'detail'
      ? directoryRows.find((row) => row.actor === panel.agentId || row.uri === panel.agentId)
      : undefined;
  const extensionViews: Record<string, AgentExtensionView> = Object.fromEntries(
    agents.map((agent) => [
      agent.id,
      directoryMode
        ? (directoryExtensions[agent.id] ?? getAgentExtension(undefined, agent.id))
        : getAgentExtension(mergedExtensions, agent.id),
    ]),
  );
  const detailExtension: AgentExtensionView | undefined =
    detailAgent ? extensionViews[detailAgent.id] : undefined;

  function navigateToConnections(): void {
    dispatch({ type: 'navigate', page: 'connections' });
  }

  useEffect(() => {
    if (panel.kind === 'detail' && !selectedAgent && canConfirmMissingAgent) {
      notFoundTitleRef.current?.focus();
    }
  }, [panel, selectedAgent, canConfirmMissingAgent]);

  // Focus the list title only once the list has really mounted.
  useEffect(() => {
    if (focusListWhenMounted && panel.kind === 'list') {
      listHeadingRef.current?.focus();
      setFocusListWhenMounted(false);
    }
  }, [focusListWhenMounted, panel.kind]);

  function backToList(): void {
    setFormMessage(null);
    setCreatedNotice(null);
    setFocusListWhenMounted(true);
    dispatch({ type: 'close-agent-panel' });
  }

  function credentialErrorMessage(error: unknown): string {
    if (error instanceof Error && error.name === 'ProviderLoginFileError') {
      const code = (error as { readonly code?: string }).code;
      if (code === 'LOGIN_FILES_CANCELLED') {
        return '已取消选择登录文件';
      }
    }
    return agentFailureMessage(toAgentRequestError(error)).text;
  }

  async function applyCredential(
    result: Extract<AgentMutationResult, { kind: 'created' }>,
    credential: AgentCredentialPlan,
  ): Promise<{ readonly tone: 'status' | 'error'; readonly text: string }> {
    const allowProvider = directoryApi?.allowProvider?.bind(directoryApi);
    const loginNative = directoryApi?.loginNative?.bind(directoryApi);
    if (!directoryApi || result.actorUri === undefined) {
      return {
        tone: 'error',
        text: 'Agent 已创建，但未拿到 actor，凭据未配置。请到“凭据”重试。',
      };
    }
    try {
      if (credential.kind === 'key' && allowProvider !== undefined) {
        const outcome = await allowProvider(
          result.actorUri,
          credential.provider,
          { keyId: credential.keyId, grantId: `model-${credential.provider}`, revision: 1 },
        );
        return {
          tone: 'status',
          text: outcome.changed
            ? 'Agent 创建已完成，Key 已授权。'
            : 'Agent 创建已完成；该 Key 授权未变化。',
        };
      }
      if (credential.kind === 'auth' && loginNative !== undefined) {
        if (credential.files.length === 0) {
          return {
            tone: 'error',
            text: 'Agent 已创建，但登录文件未选择。请到“凭据”重试。',
          };
        }
        await loginNative(result.actorUri, credential.harness, credential.files);
        return {
          tone: 'status',
          text: 'Agent 创建已完成，登录文件已写入。',
        };
      }
      return credential.kind === 'none'
        ? { tone: 'status', text: 'Agent 创建已完成。' }
        : { tone: 'error', text: 'Agent 已创建，凭据未配置。' };
    } catch (error) {
      if (methodRefusal(error) !== null) {
        return { tone: 'error', text: 'Agent 已创建，凭据未配置。' };
      }
      const detail = credentialErrorMessage(error).replace(/[。.]$/u, '');
      return {
        tone: 'error',
        text: `Agent 已创建，但凭据未完成：${detail}。请到“凭据”重试。`,
      };
    }
  }

  function applyResult(
    result: AgentMutationResult,
    draft?: AgentDraft,
    notice?: CreatedNotice,
  ): void {
    if (result.kind === 'created') {
      setLocalAgents([
        ...agents.filter((agent) => agent.id !== result.agent.id),
        result.agent,
      ]);
      if (draft?.preferredHarness) {
        setLocalExtensions((current) => ({
          ...current,
          [result.agent.id]: {
            preferredHarness: draft.preferredHarness ?? undefined,
            model: draft.model ?? undefined,
          },
        }));
      }
      setFormMessage(null);
      setCreatedNotice(notice && (notice.tone === 'error' || result.created === false)
        ? { ...notice, agentId: result.agent.id } : null);
      dispatch({ type: 'open-agent-detail', agentId: result.agent.id });
      return;
    }
    if (result.kind === 'updated') {
      setLocalAgents(
        (directoryMode ? directoryViews : (localAgents ?? baseAgents)).map(
          (agent) => (agent.id === result.agent.id ? result.agent : agent),
        ),
      );
    }
  }

  function openCreate(): void {
    const refusal = accessRefusal(methodAccess, 'agent.create');
    if (refusal) {
      notifyUnavailable('创建 Agent', refusal);
      return;
    }
    setFormMessage(null);
    setCreatedNotice(null);
    dispatch({ type: 'open-agent-create' });
  }

  async function createAgent(draft: AgentDraft): Promise<void> {
    if (gatewayClient === undefined) return;
    const refusal = accessRefusal(methodAccess, 'agent.create');
    if (refusal) {
      notifyUnavailable('创建 Agent', refusal);
      return;
    }
    setPending(true);
    const result = await gatewayClient.create(draft);
    if (result.kind === 'refused') {
      setPending(false);
      notifyUnavailable('创建 Agent', result.refusal);
      return;
    }
    let notice: CreatedNotice | undefined;
    if (result.kind === 'incomplete') {
      setPending(false); setCreateUnknown(true); directory.reload();
      setFormMessage({ tone: 'error', text: result.message });
      setCreatedNotice({ tone: 'error', text: result.message, agentId: result.actorUri });
      dispatch({ type: 'open-agent-detail', agentId: result.actorUri }); return;
    }
    if (result.kind === 'created') {
      if (result.created === false) {
        notice = { tone: 'status', text: '已复用现有 Agent；未改动其凭据。' };
      } else if (draft.requireModelConfiguration) {
        notice = { tone: 'error', text: 'Agent 身份已创建，尚未配置模型；请导入登录文件、选择登录目标并保存配置后再启动。' };
      } else {
        const credential = draft.credential;
        if (credential !== undefined && credential.kind !== 'none') {
          const credentialNotice = await applyCredential(result, credential);
          notice = credentialNotice;
          directory.reload();
        }
      }
    }
    setPending(false);
    setCreateUnknown(result.kind === 'unknown');
    applyResult(result, draft, notice);
    if (result.kind === 'failed' || result.kind === 'unknown') {
      setFormMessage({ tone: 'error', text: result.message });
    }
  }

  const update = gatewayClient?.update?.bind(gatewayClient);

  async function updateAgent(
    agentId: string,
    draft: AgentDraft,
  ): Promise<AgentMutationResult> {
    if (renameAgent !== undefined) {
      try {
        const row = await renameAgent(agentId, draft.displayName);
        directory.reload();
        const result: AgentMutationResult = {
          kind: 'updated',
          agent: directoryRowToView(row),
        };
        applyResult(result);
        return result;
      } catch (error) {
        const refusal = methodRefusal(error);
        if (refusal) {
          return { kind: 'refused', refusal };
        }
        const failure = toAgentRequestError(error);
        const message = agentFailureMessage(failure);
        return failure.dispatch === 'unknown'
          ? {
              kind: 'unknown',
              message: message.text,
              code: failure.code,
              diagnostics: failure.diagnostics,
            }
          : {
              kind: 'failed',
              message: message.text,
              code: failure.code,
              diagnostics: failure.diagnostics,
            };
      }
    }
    const result = await update!(agentId, draft);
    applyResult(result);
    return result;
  }

  async function deleteAgent(agentId: string, operationId: string): Promise<AgentMutationResult> {
    let result: AgentMutationResult;
    if (!directoryApi?.destroy) throw new TypeError('Agent destruction has no handler');
    try {
      await directoryApi.destroy(operationId);
      result = { kind: 'deleted', agentId };
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) return { kind: 'refused', refusal };
      const failure = toAgentRequestError(error);
      const uncertain = failure.dispatch === 'unknown' || [
        'SUBMIT_OUTCOME_UNKNOWN', 'DOMAIN_COMMAND_TIMEOUT', 'AGENT_DESTROY_SETTLEMENT_UNKNOWN',
        'OPERATION_IN_PROGRESS', 'LIFECYCLE_OPERATION_UNSETTLED',
      ].includes(failure.code);
      directory.reload();
      return { kind: uncertain ? 'unknown' : 'failed', code: failure.code, diagnostics: failure.diagnostics,
        message: uncertain ? '删除结果未确认，请核对 Agent 列表；不会自动重试。'
          : ['PREVIEW_EXPIRED', 'PREVIEW_STALE', 'PREVIEW_UNKNOWN'].includes(failure.code)
            ? '删除预览已失效，请重新打开删除窗口核对。'
            : `删除未完成（${failure.code}）。` };
    }
    if (result.kind === 'deleted') {
      const targetId = directoryRows.find(row => row.actor === agentId)?.uri ?? agentId;
      dispatch({ type: 'agent-deleted', targetId });
      directory.reload();
      setLocalAgents(
        (directoryMode ? directoryViews : (localAgents ?? baseAgents)).filter(
          (agent) => agent.id !== agentId,
        ),
      );
      setFocusListWhenMounted(true);
      dispatch({ type: 'close-agent-panel' });
    }
    return result;
  }

  const organizationDirectoryReady = orgSnapshot.orgAgentDirectory !== undefined;
  const directoryPending = directoryMode && directory.status !== 'ready' && !organizationDirectoryReady;
  const directoryErrorCode = directoryMode
    ? /(?:^|\D)([45]\d{2})(?:\D|$)/u.exec(directory.error ?? '')?.[1] ?? null
    : null;

  return (
    <section className="page page-contacts" data-page="contacts">
      {panel.kind === 'list' ? (
        <h1 className="page-title">Agent</h1>
      ) : (
        <>
          <h1 className="sr-only">Agent</h1>
          <nav className="page-breadcrumb" aria-label="面包屑">
            <ol>
              <li>
                <a
                  href="#agents"
                  onClick={(event) => {
                    event.preventDefault();
                    backToList();
                  }}
                >
                  Agent
                </a>
              </li>
              <li aria-current="page">
                {panel.kind === 'create'
                  ? '创建 Agent'
                  : '配置'}
              </li>
            </ol>
          </nav>
        </>
      )}

      {createdNotice && panel.kind === 'detail' && panel.agentId === createdNotice.agentId && (
        <div
          className="agent-form-message"
          role={createdNotice.tone === 'error' ? 'alert' : 'status'}
        >
          <span>{createdNotice.text}</span>
        </div>
      )}

      {directoryMode && directory.status === 'ready' && directory.error && (
        <p role="alert">Agent 状态更新失败，当前显示上次读取的结果。
          <Button variant="text" onClick={() => directory.reload()}>重试</Button>
        </p>
      )}

      {panel.kind === 'create' && (
        <AgentForm
          mode="create"
          initial={{
            displayName: '',
            role: '',
            modelServiceId: '',
            organizationId: null,
          }}
          organizations={organizations}
          previewMode={!directoryMode}
          explicitModel={directoryApi?.configureModel !== undefined}
          credentialsApi={directoryMode ? createKeyCredentials : undefined}
          pickLoginFiles={directoryMode ? createLoginFilePicker : undefined}
          pending={pending}
          submitDisabled={createUnknown}
          submitDisabledReason="上次创建结果未知；请先核对 Agent 列表，确认后再重新打开创建，不会自动重试。"
          message={formMessage}
          onSubmit={(draft) => void createAgent(draft)}
          onCancel={() => {
            setCreateUnknown(false);
            setFormMessage(null);
            backToList();
          }}
        />
      )}

      {panel.kind === 'detail' && detailAgent && (
        <AgentDetail
          key={detailAgent.id}
          agent={detailAgent}
          extension={detailExtension ?? getAgentExtension(undefined, detailAgent.id)}
          organizations={organizations}
          onNavigateConnections={navigateToConnections}
          onSave={directoryMode ? (renameAgent !== undefined ? (draft) => updateAgent(detailAgent.id, draft) : undefined)
            : update !== undefined
              ? (draft) => updateAgent(detailAgent.id, draft)
              : undefined}
          onDelete={directoryApi?.previewDestroy && directoryApi?.destroy
            ? (operationId) => deleteAgent(detailAgent.id, operationId) : undefined}
          onStartChat={
            !chatAvailable ? undefined : isCanonicalAgentUri(detailDirectoryRow?.uri ?? detailAgent.id)
              ? () =>
                  dispatch({
                    type: 'open-agent-chat',
                    targetId: detailDirectoryRow?.uri ?? detailAgent.id,
                  })
              : state.targets.some((target) => target.id === detailAgent.id)
                ? () => dispatch({ type: 'open-chat', targetId: detailAgent.id })
                : undefined
          }
          onRuntimeAction={
            runtimeActionHandler && detailAgent
              ? (action, options) => runtimeActionHandler(action, detailAgent.id, options)
              : undefined
          }
          checkAccess={directoryMode ? (method) => accessRefusal(methodAccess, method) : undefined}
          onRefreshRuntime={directoryMode ? directory.reload : undefined}
          agentsApi={directoryApi}
          directoryRows={directoryMode ? directory.rows : undefined}
          credentialsApi={directoryMode ? tabCredentials : undefined}
          nativeAuthApi={directoryMode ? nativeAuthApi : undefined}
          keysReadable={callable('agent.secret.provider-list')}
          nativeLoginFiles={directoryMode ? nativeLoginFiles : undefined}
          showOrganization={!directoryMode}
          previewMode={!directoryMode}
          adapterBindings={adapterBindings}
          agentActorUri={detailDirectoryRow?.uri}
          agentLabels={
            directoryMode
              ? directory.rows.map((row) => ({ uri: row.uri, displayName: row.displayName }))
              : agents.map((agent) => ({ uri: agent.id, displayName: agent.displayName }))
          }
          orgSections={
            directoryMode
              ? undefined
              : (
                <AgentOrgPanel
                  agentId={detailAgent.id}
                  agentName={detailAgent.displayName}
                  dispatch={dispatch}
                />
              )
          }
        />
      )}

      {panel.kind === 'detail' && selectedAgent?.remote && (
        <Card as="section" className="card agent-panel agent-not-found" role="status">
          <h2>远端 Agent 详情</h2>
          <p className="copy">{selectedAgent.displayName} 属于当前组织，本机没有它的运行与管理资料。</p>
          {remoteChatTargets[selectedAgent.agentId ?? selectedAgent.id] && (
            <Button variant="primary" onClick={() => {
              const targetId = remoteChatTargets[selectedAgent.agentId ?? selectedAgent.id];
              void capabilities.chatTargets?.('agent').then((rows) => {
                if (rows.some((row) => row.targetUri === targetId && row.deliverable)) {
                  dispatch({ type: 'open-agent-chat', targetId });
                } else {
                  setRemoteChatTargets({});
                }
              }).catch(() => setRemoteChatTargets({}));
            }}>开始对话</Button>
          )}
          <Button variant="secondary" onClick={backToList}>返回 Agent 列表</Button>
        </Card>
      )}

      {panel.kind === 'detail' && !selectedAgent && canConfirmMissingAgent && (
        <Card
          as="section"
          className="card agent-panel agent-not-found"
          role="status"
          aria-labelledby="agent-not-found-title"
        >
          <h2 id="agent-not-found-title" ref={notFoundTitleRef} tabIndex={-1}>
            {AGENT_NOT_FOUND}
          </h2>
          <p className="copy">该 Agent 可能已被删除，或列表状态已变化。</p>
        </Card>
      )}

      {panel.kind !== 'create' && directoryMode && !organizationDirectoryReady && (directory.status === 'loading' || directory.status === 'unwired') && (
        <Card padding="lg" className="card contacts-card" role="status" aria-busy="true">
          <p className="agent-form-message">Agent 列表加载中…</p>
        </Card>
      )}

      {panel.kind !== 'create' && directoryMode && directory.status === 'error' && (
        <Card padding="lg" className="card contacts-card" role="alert">
          <h2>{organizationDirectoryReady ? '本机 Agent 列表读取失败' : 'Agent 列表读取失败'}</h2>
          <p className="copy">
            {directoryErrorCode
              ? '可以先重试；持续失败请联系管理员，并带上错误码。'
              : '可以先重试；持续失败请联系管理员。'}
          </p>
          {directoryErrorCode && (
            <p className="agent-diagnostic-code">错误码 {directoryErrorCode}</p>
          )}
          <Button
            variant="secondary"
            onClick={directory.reload}
          >
            重试
          </Button>
        </Card>
      )}

      {panel.kind === 'list' && !directoryPending && (
        <AgentList
          availabilityMode={directoryMode ? 'runtime' : 'connection'}
          agents={visibleAgents}
          total={agents.length}
          query={state.agentQuery}
          filter={state.agentFilter}
          extensions={extensionViews}
          headingRef={listHeadingRef}
          ownerFilter={ownerFilter}
          hostFilter={hostFilter}
          ownerOptions={ownerOptions}
          hostOptions={hostOptions}
          onOwnerFilter={setOwnerFilter}
          onHostFilter={setHostFilter}
          onCreate={gatewayClient !== undefined ? openCreate : undefined}
          onOpen={(agentId) =>
            dispatch({ type: 'open-agent-detail', agentId })
          }
          onQuery={(value) => dispatch({ type: 'set-agent-query', value })}
          onFilter={(filter) => dispatch({ type: 'set-agent-filter', filter })}
          onClearFilters={() => {
            setOwnerFilter('');
            setHostFilter('');
            dispatch({ type: 'clear-agent-filters' });
          }}
        />
      )}
    </section>
  );
}
