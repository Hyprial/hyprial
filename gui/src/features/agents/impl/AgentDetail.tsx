import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import type {
  AgentOrganization,
  ManagedAgentView,
} from '../../../state/agent-selectors';
import type { AgentDraft, AgentMutationResult } from './AgentMutationGateway';
import type {
  AgentDirectoryRow,
  AgentStartOptions,
  AgentsApi,
} from './api';
import {
  useAgentProviderStatus,
} from './api/use-agent-directory';
import type { SharedCredentialsApi } from '../../connections';
import type { NativeAuthApi, ProviderLoginFile } from '../../agent-provider';
import type { AgentExtensionView, AgentProviderGrantView } from './agent-extensions';
import { AGENT_RUNTIME_LABELS, runtimeTone } from './agent-extensions';
import { AgentDeleteDialog } from './destroy/AgentDeleteDialog';
import { AgentForm, agentDraftOf, agentNameError } from './AgentForm';
import { AgentAvatar } from '../../../components/AgentAvatar';
import { Badge } from '../../../components/Badge';
import { Button, Dialog } from '../../../components/primitives';
import { useNotifyUnavailable } from '../../../components/notifications';
import type { MethodRefusal } from '../../../client';
import { AgentOrgSummary } from '../../org';
import { CredentialsTab } from './detail/CredentialsTab';
import { PlatformBindingsTab } from './detail/PlatformBindingsTab';
import { OverviewTab } from './detail/OverviewTab';
import { RelationsTab } from './detail/RelationsTab';
import {
  DETAIL_TAB_IDS,
  DETAIL_TAB_LABELS,
  overviewNotices,
  type DetailTabId,
} from './detail/agent-detail-format';
import type { ProviderLoginState } from './agent-extensions';
import type { RuntimeActionOutcome } from './api/contract-gateway';
import type { AgentAdapterBindingsApi } from './api/agent-adapter-bindings';

export function defaultStartModel(
  harness: string | null | undefined,
  provider: string | null | undefined,
): string {
  return provider === 'deepseek' && (harness === 'pi' || harness === 'codex')
    ? 'deepseek-flash'
    : '';
}

export interface AgentDetailProps {
  readonly agent: ManagedAgentView;
  readonly extension: AgentExtensionView;
  readonly organizations: readonly AgentOrganization[];
  readonly onNavigateConnections: () => void;
  readonly onSave?: (draft: AgentDraft) => Promise<AgentMutationResult>;
  readonly onDelete?: (operationId: string) => Promise<AgentMutationResult>;
  /**
   * Open the chat page with this agent as the active target. Undefined when
   * the agent has no chat-capable target (unbound credentials or an unknown
   * chat target) — the button then renders disabled instead of pretending.
   */
  readonly onStartChat?: () => void;
  /** dev-04：详情中的组织分区（所属组织、通信、迁移），渲染进关联页签。 */
  readonly orgSections?: ReactNode;
  /** 逐 provider 用户登录状态（G4-4）；缺席 = 无数据源，不渲染登录链接。 */
  readonly providerLogins?: Readonly<Record<string, ProviderLoginState>>;
  /** implG2：已接线时透传停止（生成 client）；启动可带本次 harness/provider/model。 */
  readonly onRuntimeAction?: (
    action: 'start' | 'stop',
    options?: AgentStartOptions,
  ) => Promise<RuntimeActionOutcome>;
  /**
   * Asked before a control opens an editor, dialog or busy state: a catalog
   * refusal is notified once and nothing starts or opens. Absent when the
   * composition has no catalog to ask.
   */
  readonly checkAccess?: (method: string) => MethodRefusal | null;
  /** Whether agent.secret.provider-list is callable; false = the credentials tab has no Key source. */
  readonly keysReadable?: boolean;
  readonly onRefreshRuntime?: () => void;
  /** 目录读取 api。 */
  readonly agentsApi?: AgentsApi;
  readonly directoryRows?: readonly AgentDirectoryRow[];
  /** 账号 Key 列表与更新入口（Agent 设置 → 凭据的同一 authority）。 */
  readonly credentialsApi?: SharedCredentialsApi;
  readonly nativeAuthApi?: NativeAuthApi;
  /** 自有登录文件选择器；缺省时只显示不可用原因。 */
  readonly nativeLoginFiles?: (
    actor: string,
    provider: string,
  ) => Promise<readonly ProviderLoginFile[]>;
  /** live 组合没有组织关系方法；false 时隐藏组织展示。 */
  readonly showOrganization?: boolean;
  /** fixture 组合保留职责/组织等演示字段；目录组合只显示真实契约字段。 */
  readonly previewMode?: boolean;
  /** 平台绑定：agent cell 的 adapter 成员与路由可用性。 */
  readonly adapterBindings?: AgentAdapterBindingsApi;
  /** 绑定时使用的 Agent canonical actor URI；缺省回退到 agent.id。 */
  readonly agentActorUri?: string;
  /** 绑定展示用的 Agent 名称；live 用目录行，fixture 用本地 Agent。 */
  readonly agentLabels?: readonly { readonly uri: string; readonly displayName: string }[];
  readonly providerDefault?: { readonly ready: boolean; readonly provider: string | null };
  readonly secretRows?: readonly unknown[];
}

type Mode = 'view' | 'edit';

export function AgentDetail({
  agent,
  extension,
  organizations,
  onNavigateConnections,
  onSave,
  onDelete,
  onStartChat,
  orgSections,
  onRuntimeAction,
  checkAccess,
  keysReadable = true,
  onRefreshRuntime,
  agentsApi,
  directoryRows = [],
  credentialsApi,
  nativeAuthApi,
  nativeLoginFiles,
  showOrganization = true,
  previewMode = false,
  adapterBindings,
  agentActorUri,
  agentLabels,
  providerDefault: _providerDefault,
  secretRows: _secretRows,
}: AgentDetailProps) {
  const [mode, setMode] = useState<Mode>('view');
  const [requestedTab, setTab] = useState<DetailTabId>('overview');
  const notifyUnavailable = useNotifyUnavailable();
  const tabs = DETAIL_TAB_IDS.filter(
    (id) =>
      id === 'overview'
      || (id === 'bindings' && adapterBindings !== undefined)
      || (id === 'credentials' && agentsApi !== undefined)
      || (id === 'relations' && showOrganization && orgSections !== undefined),
  );
  const tab = tabs.includes(requestedTab) ? requestedTab : 'overview';
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<
    { tone: 'error' | 'status'; text: string } | null
  >(null);
  const [runtimeOutcome, setRuntimeOutcome] = useState<
    { tone: 'status' | 'error'; text: string } | null
  >(null);
  const [runtimePending, setRuntimePending] = useState(false);
  const [credentialReadRevision, setCredentialReadRevision] = useState(0);
  const [runtimeIntent, setRuntimeIntent] = useState<'start' | 'stop' | null>(null);
  const [runtimeAwaiting, setRuntimeAwaiting] = useState(false);
  const [startOpen, setStartOpen] = useState(false);
  const [startHarness, setStartHarness] = useState('');
  const [startProvider, setStartProvider] = useState('');
  const [startModel, setStartModel] = useState('');
  const [startCwd, setStartCwd] = useState('');
  const [startApproved, setStartApproved] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [renameError, setRenameError] = useState<string | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);
  const loadDeletePreview = useCallback(() => agentsApi!.previewDestroy!(agent.id), [agentsApi, agent.id]);

  const directoryRow =
    directoryRows.find((row) => row.actor === agent.id) ?? null;
  const piStart = (startHarness || directoryRow?.preferredHarness || extension.preferredHarness) === 'pi';
  const needsStartApproval = piStart && (!startApproved || !startCwd.trim());
  const incompletePiModel = piStart && Boolean(startProvider.trim()) !== Boolean(startModel.trim());
  const startModelError = incompletePiModel ? '请同时填写服务商和模型。'
    : startProvider.trim() === 'hyprial' && !/^[^/\s]+\/[^/\s]+$/.test(startModel.trim())
      ? '模型格式：服务商/模型。'
      : startHarness === 'codex' && startProvider.trim() === 'deepseek'
        && startModel.trim() !== 'deepseek-flash' ? 'Codex 的 DeepSeek 模型仅支持 deepseek-flash。'
        : null;
  const expectedAgent =
    directoryRow && directoryRow.createdAtMs !== null
      ? { uri: directoryRow.uri, createdAtMs: directoryRow.createdAtMs }
      : null;
  const startupFailure = directoryRow?.startupFailure;
  const priorFailureRef = useRef<string | undefined>(undefined);
  const runtimeLabel = startupFailure ? '启动失败' : extension.runtimeDetail
    ? extension.runtime === 'unknown'
      ? extension.runtimeDetail
      : extension.runtimeDetail === AGENT_RUNTIME_LABELS[extension.runtime]
        ? AGENT_RUNTIME_LABELS[extension.runtime]
        : `${AGENT_RUNTIME_LABELS[extension.runtime]} · ${extension.runtimeDetail}`
    : AGENT_RUNTIME_LABELS[extension.runtime];
  const providerStatus = useAgentProviderStatus(
    agentsApi,
    directoryRow?.uri ?? agent.id,
    mode === 'view',
    expectedAgent,
  );
  const keyGrants: readonly AgentProviderGrantView[] =
    providerStatus.view.status === 'ready'
      ? providerStatus.view.rows.map((grant) => ({
          provider: grant.provider,
          state: grant.state,
          source: 'user-granted' as const,
        }))
      : extension.providerGrants;
  const notices = overviewNotices(keyGrants);
  if (startupFailure) notices.unshift({ key: 'startup-failure', tone: 'warning',
    chip: '启动失败', text: startupFailure.message, jumpTo: 'overview' });

  useEffect(() => {
    titleRef.current?.focus();
  }, [agent.id]);

  useEffect(() => {
    if ((runtimeIntent === 'start' && extension.runtime === 'online')
      || (runtimeIntent === 'stop' && extension.runtime === 'offline')
      || (startupFailure && startupFailure.operationId !== priorFailureRef.current)) setRuntimeAwaiting(false);
  }, [runtimeAwaiting, runtimeIntent, extension.runtime, startupFailure]);

  function closeConfirm(): void {
    setConfirmOpen(false);
    deleteButtonRef.current?.focus();
  }

  async function save(draft: AgentDraft): Promise<void> {
    if (!onSave) return;
    setPending(true);
    setMessage(null);
    const result = await onSave(draft);
    setPending(false);
    if (result.kind === 'refused') {
      notifyUnavailable('更新 Agent', result.refusal);
      return;
    }
    if (result.kind === 'updated') {
      setMode('view');
      setMessage({ tone: 'status', text: '已保存修改' });
      return;
    }
    if (result.kind === 'created' || result.kind === 'deleted') {
      setMessage({ tone: 'status', text: '已更新' });
      return;
    }
    setMessage({
      tone: 'error',
      text:
        result.kind === 'unknown'
          ? `${result.message} 请先核对详情。`
          : result.message,
    });
  }

  function startRename(): void {
    const refusal = checkAccess?.('agent.rename');
    if (refusal) {
      notifyUnavailable('重命名 Agent', refusal);
      return;
    }
    setRenameValue(agent.displayName);
    setRenameError(null);
    setRenaming(true);
  }

  async function saveRename(): Promise<void> {
    const next = renameValue.trim();
    const invalidName = agentNameError(next);
    if (invalidName) {
      setRenameError(invalidName);
      return;
    }
    if (next === agent.displayName || !onSave) {
      setRenaming(false);
      return;
    }
    setPending(true);
    setMessage(null);
    const result = await onSave({
      ...agentDraftOf(agent),
      displayName: next,
      provider: directoryRow?.provider ?? null,
      model: directoryRow?.model ?? null,
      preferredHarness:
        directoryRow?.preferredHarness ?? extension.preferredHarness ?? null,
    });
    setPending(false);
    if (result.kind === 'refused') {
      notifyUnavailable('重命名 Agent', result.refusal);
      return;
    }
    if (result.kind === 'updated') {
      setRenaming(false);
      setRenameError(null);
      setMessage({ tone: 'status', text: '已重命名' });
      return;
    }
    setRenameError(
      result.kind === 'failed' || result.kind === 'unknown'
        ? result.message
        : '重命名未完成',
    );
  }

  async function confirmDelete(operationId: string): Promise<void> {
    if (!onDelete) return;
    setPending(true);
    setMessage(null);
    const result = await onDelete(operationId);
    setPending(false);
    setConfirmOpen(false);
    if (result.kind === 'deleted') {
      return;
    }
    if (result.kind === 'refused') {
      notifyUnavailable('删除 Agent', result.refusal);
      return;
    }
    if (result.kind === 'created' || result.kind === 'updated') {
      setMessage({ tone: 'status', text: '已更新' });
      return;
    }
    setMessage({
      tone: 'error',
      text:
        result.kind === 'unknown'
          ? `${result.message} 请先核对列表。`
          : result.message,
    });
  }

  async function start(): Promise<void> {
    if (runtimePending || runtimeAwaiting || !onRuntimeAction || startModelError || needsStartApproval || extension.runtime === 'online') {
      return;
    }
    setRuntimeOutcome(null);
    setStartOpen(false);
    setRuntimePending(true);
    priorFailureRef.current = startupFailure?.operationId;
    setRuntimeIntent('start');
    try {
      const outcome = await onRuntimeAction('start', {
        harness: startHarness || directoryRow?.preferredHarness || null,
        provider: startProvider.trim() || null,
        model: startModel.trim() || null,
        cwd: startCwd.trim() || null,
        ...(piStart ? { approve: startApproved } : {}),
      });
      if (outcome.refusal) {
        notifyUnavailable('启动 Agent', outcome.refusal);
        return;
      }
      setRuntimeOutcome(outcome);
      setRuntimeAwaiting(outcome.awaitingObservation === true);
    } finally {
      setRuntimePending(false);
      setCredentialReadRevision(value => value + 1);
    }
  }

  async function stop(): Promise<void> {
    if (runtimePending || runtimeAwaiting) {
      return;
    }
    setRuntimeOutcome(null);
    if (!onRuntimeAction) {
      return;
    }
    const refusal = checkAccess?.('agent.stop');
    if (refusal) {
      notifyUnavailable('停止 Agent', refusal);
      return;
    }
    setRuntimePending(true);
    setRuntimeIntent('stop');
    try {
      const outcome = await onRuntimeAction('stop');
      if (outcome.refusal) {
        notifyUnavailable('停止 Agent', outcome.refusal);
        return;
      }
      setRuntimeOutcome(outcome);
      setRuntimeAwaiting(outcome.awaitingObservation === true);
    } finally {
      setRuntimePending(false);
      setCredentialReadRevision(value => value + 1);
    }
  }

  function openStart(): void {
    if (runtimePending || runtimeAwaiting || extension.runtime === 'online') return;
    const refusal = checkAccess?.('agent.start');
    if (refusal) {
      notifyUnavailable('启动 Agent', refusal);
      return;
    }
    setStartHarness(directoryRow?.preferredHarness ?? extension.preferredHarness ?? '');
    const grants = providerStatus.view.status === 'ready'
      ? providerStatus.view.rows.filter(grant => grant.state === 'ok') : [];
    const harness = directoryRow?.preferredHarness ?? extension.preferredHarness;
    const provider = directoryRow?.provider ?? (grants.length === 1 ? grants[0].provider : '');
    setStartProvider(provider);
    setStartModel(directoryRow?.model?.trim() || defaultStartModel(harness, provider));
    setStartCwd(directoryRow?.cwd ?? agent.activity?.homePath ?? '');
    setStartApproved(false);
    setStartOpen(true);
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') {
      return;
    }
    event.preventDefault();
    const offset = event.key === 'ArrowRight' ? 1 : -1;
    const index = tabs.indexOf(tab);
    const next = tabs[(index + offset + tabs.length) % tabs.length];
    setTab(next);
    document.getElementById(`agent-tab-${next}`)?.focus();
  }

  if (mode === 'edit' && previewMode) {
    return (
      <section
        className="card agent-panel agent-detail"
        data-selected="true"
        data-testid="agent-detail-panel"
        data-agent-id={agent.id}
        aria-labelledby="agent-detail-title"
      >
        <AgentForm
          mode="edit"
          initial={{
            ...agentDraftOf(agent),
            provider: directoryRow?.provider ?? null,
            model: directoryRow?.model ?? null,
            preferredHarness:
              directoryRow?.preferredHarness ?? extension.preferredHarness ?? null,
          }}
          organizations={organizations}
          previewMode={previewMode}
          pending={pending}
          message={message}
          onSubmit={(draft) => void save(draft)}
          onCancel={() => {
            setMode('view');
            setMessage(null);
          }}
        />
      </section>
    );
  }

  return (
    // 原生 section：测试与共享选择器以 [data-selected="true"] 定位详情卡，
    // primitives Card 会覆写 data-selected，故此处不迁 Card 基件。
    <section
      className="card agent-panel agent-detail"
      data-selected="true"
      data-testid="agent-detail-panel"
      data-agent-id={agent.id}
      aria-labelledby="agent-detail-title"
    >
      <header className="agent-detail-head">
        <div className="agent-detail-who">
          <AgentAvatar
            name={agent.displayName}
            tone={agent.avatarTone}
            size="lg"
          />
          <div className="agent-detail-title">
            <h2 id="agent-detail-title" ref={titleRef} tabIndex={-1} className={renaming ? 'sr-only' : undefined}>
              {agent.displayName}
              {extension.preset && (
                <span className="badge badge-preset">预配置</span>
              )}
            </h2>
            {renaming ? (
              <form
                className="agent-rename-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void saveRename();
                }}
              >
                <input
                  className="ui-native-control"
                  aria-label="Agent 名称"
                  autoFocus
                  value={renameValue}
                  disabled={pending}
                  onChange={(event) => setRenameValue(event.target.value)}
                />
                <Button type="submit" variant="secondary" size="compact" loading={pending}>
                  保存
                </Button>
                <Button
                  type="button"
                  variant="text"
                  size="compact"
                  disabled={pending}
                  onClick={() => {
                    setRenaming(false);
                    setRenameError(null);
                  }}
                >
                  取消
                </Button>
                {renameError && (
                  <p className="agent-field-error" role="alert">{renameError}</p>
                )}
              </form>
            ) : (
              <p>
                {agent.role}
                {!previewMode && onSave && (
                  <Button variant="text" size="compact" onClick={startRename}>
                    重命名
                  </Button>
                )}
              </p>
            )}
          </div>
          <div className="agent-detail-actions">
            <span
              className="agent-runstate"
              data-testid="agent-detail-runstate"
            >
              <Badge tone={runtimeTone(extension.runtime)}>
                {runtimeLabel}
              </Badge>
            </span>
            {onRuntimeAction && (
              <Button variant="secondary"
                disabled={runtimePending || (runtimeAwaiting && !onRefreshRuntime)}
                loading={runtimePending}
                onClick={() => runtimeAwaiting ? onRefreshRuntime?.() : extension.runtime === 'online' ? void stop() : openStart()}>
                {runtimePending ? runtimeIntent === 'start' ? '启动中…' : '停止中…'
                  : runtimeAwaiting ? '刷新状态' : extension.runtime === 'online' ? '停止' : '启动'}
              </Button>
            )}
            {onStartChat && (
              <Button variant="primary" onClick={() => onStartChat()}>
                开始对话
              </Button>
            )}
            {previewMode && onSave && (
              <Button
                variant="secondary"
                onClick={() => setMode('edit')}
              >
                编辑
              </Button>
            )}
            {onDelete && agentsApi?.previewDestroy && agentsApi?.destroy && (
              <Button variant="secondary"
                ref={deleteButtonRef}
                type="button"
                onClick={() => {
                  const refusal = checkAccess?.('agent.destroy.preview') ?? checkAccess?.('agent.destroy');
                  if (refusal) {
                    notifyUnavailable('删除 Agent', refusal);
                    return;
                  }
                  setConfirmOpen(true);
                }}
              >
                删除
              </Button>
            )}
          </div>
        </div>

        <dl className="agent-detail-facts">
          <div>
            <dt>所有者</dt>
            <dd data-owner={agent.ownerLabel}>{agent.ownerLabel}</dd>
          </div>
          <div>
            <dt>主机</dt>
            <dd data-host={agent.host}>{agent.host}</dd>
          </div>
          {showOrganization && orgSections === undefined && (
            <div>
              <dt>所属组织</dt>
              <dd>
                <AgentOrgSummary agentId={agent.agentId ?? agent.id} fallback={agent.organizationLabel} />
              </dd>
            </div>
          )}
          {agent.channel.kind !== 'unknown' && (
            <div>
              <dt>平台绑定</dt>
              <dd data-connection="channel">
                {agent.channel.label}
                <span className="agent-detail-source">{agent.channel.sourceLabel}</span>
              </dd>
            </div>
          )}
        </dl>

        {(runtimeOutcome || startupFailure) && (
          <p
            className="agent-form-message"
            data-runtime="header-result"
            role={startupFailure || runtimeOutcome?.tone === 'error' ? 'alert' : 'status'}
          >
            {startupFailure?.message ?? runtimeOutcome?.text}
          </p>
        )}

        <div
          className="agent-detail-tabs"
          role="tablist"
          aria-label="Agent 详情页签"
          onKeyDown={handleTabKeyDown}
        >
          {tabs.map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`agent-tab-${id}`}
              aria-selected={tab === id}
              aria-controls={`agent-tabpanel-${id}`}
              tabIndex={tab === id ? 0 : -1}
              className="agent-detail-tab"
              onClick={() => setTab(id)}
            >
              {DETAIL_TAB_LABELS[id]}
            </button>
          ))}
        </div>
      </header>

      {message && (
        <p
          className="agent-form-message"
          role={message.tone === 'error' ? 'alert' : 'status'}
        >
          {message.text}
        </p>
      )}

      <div
        role="tabpanel"
        id="agent-tabpanel-overview"
        aria-labelledby="agent-tab-overview"
        hidden={tab !== 'overview'}
      >
        <OverviewTab
          extension={extension}
          notices={notices}
          onJumpTo={setTab}
          activity={agent.activity}
          onConfigureStart={onRuntimeAction ? openStart : undefined}
          configureDisabled={runtimePending || runtimeAwaiting || extension.runtime === 'online'}
          availableTabs={tabs}
          noAnomalyConfirmed={providerStatus.view.status === 'ready'}
        />
      </div>

      {adapterBindings !== undefined && (
        <div
          role="tabpanel"
          id="agent-tabpanel-bindings"
          aria-labelledby="agent-tab-bindings"
          hidden={tab !== 'bindings'}
        >
          <PlatformBindingsTab
            agentActor={agentActorUri ?? agent.id}
            agentLabels={agentLabels}
            api={adapterBindings}
            checkAccess={checkAccess}
            onNavigateConnections={onNavigateConnections}
          />
        </div>
      )}

      {agentsApi !== undefined && (
        <div
          role="tabpanel"
          id="agent-tabpanel-credentials"
          aria-labelledby="agent-tab-credentials"
          hidden={tab !== 'credentials'}
        >
          <CredentialsTab
            active={tab === 'credentials'}
            actor={directoryRow?.uri ?? agent.id}
            expectedAgent={expectedAgent}
            agentsApi={agentsApi}
            providerStatus={providerStatus}
            credentialsApi={credentialsApi}
            nativeAuthApi={nativeAuthApi}
            keysReadable={keysReadable}
            checkAccess={checkAccess}
            runtimeRevision={credentialReadRevision}
            nativeLoginFiles={nativeLoginFiles}
            preferredHarness={directoryRow?.preferredHarness ?? extension.preferredHarness}
            harness={directoryRow?.harness ?? extension.harness}
            onNavigateConnections={onNavigateConnections}
          />
        </div>
      )}

      {showOrganization && orgSections !== undefined && (
        <div
          role="tabpanel"
          id="agent-tabpanel-relations"
          aria-labelledby="agent-tab-relations"
          hidden={tab !== 'relations'}
        >
          <RelationsTab orgSections={orgSections} />
        </div>
      )}

      <Dialog open={startOpen} title="启动 Agent" onClose={() => { if (!runtimePending) setStartOpen(false); }}
        actions={[
          { label: '取消', variant: 'secondary', disabled: runtimePending, onClick: () => setStartOpen(false) },
          { label: '保存并启动', variant: 'primary', loading: runtimePending,
            disabled: runtimePending || Boolean(startModelError) || needsStartApproval || extension.runtime === 'online', onClick: () => void start() },
        ]}>
          <div className="agent-config-form">
            {extension.runtime === 'online' && (
              <p className="agent-manage-note" role="status">
                Agent 正在运行；先停止再启动才能应用本次设置。
              </p>
            )}
            <label htmlFor="agent-start-harness">harness</label>
            <input
              className="ui-native-control"
              id="agent-start-harness"
              value={startHarness}
              readOnly
            />
            {(piStart || startProvider) && <>
            <label htmlFor="agent-start-provider">服务商</label>
            <input
              className="ui-native-control"
              id="agent-start-provider"
              value={startProvider}
              onChange={(event) => setStartProvider(event.target.value)}
              readOnly={!piStart}
              placeholder="例如 deepseek"
            />
            </>}
            <label htmlFor="agent-start-model">{(piStart && startProvider.trim()) || startProvider === 'hyprial'
              || (startHarness === 'codex' && startProvider === 'deepseek') ? '模型' : '模型（可选）'}</label>
            <input
              className="ui-native-control"
              id="agent-start-model"
              value={startModel}
              onChange={(event) => setStartModel(event.target.value)}
              placeholder={piStart && startProvider.trim() ? '填写该服务商的模型 ID' : '可选模型 ID'}
            />
            {!startModel && !startProvider && <p className="agent-manage-note">未指定时，由 harness 选择模型。</p>}
            {startModelError && <p className="agent-manage-note" role="status">{startModelError}</p>}
            <label htmlFor="agent-start-cwd">工作目录</label>
            <input id="agent-start-cwd" className="ui-native-control" value={startCwd}
              onChange={(event) => { setStartCwd(event.target.value); setStartApproved(false); }} placeholder="保留现有目录" />
            {piStart && <label className="agent-start-approval">
              <input type="checkbox" checked={startApproved}
                onChange={(event) => setStartApproved(event.target.checked)} />
              信任此工作目录并允许自动恢复
            </label>}
          </div>
      </Dialog>

      {confirmOpen && <AgentDeleteDialog name={agent.displayName} load={loadDeletePreview}
        confirm={confirmDelete} onClose={closeConfirm} />}

    </section>
  );
}
