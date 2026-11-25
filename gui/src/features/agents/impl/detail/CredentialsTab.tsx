/**
 * 决策记录 2026-11-11（Agent 凭据配置）。
 *
 * - 本页读取 Key 授权和更新凭据，并提供与创建表单同构的编辑入口：
 *   先选 Key / Auth，再选具体 Key 或登录文件；不把授权、切换、登录拆成并列按钮。
 * - Key 明文和撤销在凭据设置管理（agent.secret.*）；本页只写 Agent 授权关系
 *   agent.secret.grant。撤销不在这里重复。
 * - Auth 是 per-agent 原生登录文件；清单由单独的 agent.provider.auth.list 数据能力注入。
 *   导入使用 agent.provider.login，移除使用既有 agent.provider.logout；无读能力时不伪造记录。
 * - 共享 Auth（agent.secret.*）与能力台账（agent grant）都不在本页。
 * - Key 与 Auth 都在下一次启动加载；保存不删除另一类凭据、不代表完成认证切换。
 * - 已有 Auth 可沿用，完成前重读；只有显式上传替换才重新导入文件。
 * - 2026-11-14：没有写入端口时「编辑凭据」不渲染；Key 清单读取在发出前被拒绝
 *   （methodRefusal）视为没有 Key 来源，授权读取被拒绝则不渲染 Key 块；用户操作
 *   被拒绝时发出通知（保存凭据、移除登录文件），不在页内解释。
 * - 2026-11-14（验证修订）：打开编辑器之前先查方法目录——agent.secret.grant 与
 *   agent.provider.login 都被拒绝则只发一条通知、不打开；只拒绝其中一个时对话框保留，
 *   在用户选择该类凭据并保存时拒绝，忙碌态在拒绝之后才开始。移除 Auth 在打开确认框
 *   之前先查 agent.provider.logout，被拒绝不打开确认框，也不会把拒绝当作移除成功而
 *   关闭对话框或重读清单。Key 清单读取没有端口（agent.secret.provider-list 被拒绝）等于没有
 *   Key 来源；dispatched 的读取失败只显示固定文案，不渲染错误对象的 message。
 *   injected client 缺 handler 的 api 成员不存在，对应入口不渲染。
 * - 2026-11-14（凭据类型随写方法）：对话框只提供能保存的类型。Key 需要 allow 方法可调用
 *   且有兼容 Key；Auth 上传需要 login 方法可调用。沿用已有 Auth 不调用 login，只重读
 *   agent.provider.auth.list，所以 login 被拒绝时已有 Auth 仍可作为唯一的 Auth 选项。
 *   没有任何可保存类型就只发一条通知（主语 编辑凭据）、不打开；只剩一类时不显示类型
 *   选择器。auth.list 不可调用时不渲染 Auth 小节标题。
 * - 2026-11-15（合并 Agent 与连接两个范围）：Auth 读取端口是独立的 nativeAuthApi，只随
 *   agent.provider.auth.list；没有 Key 端口（credentialsApi 缺席）时 Auth 清单、沿用已有 Auth
 *   与完成前重读照常工作。未传 nativeAuthApi 时沿用 credentialsApi.nativeAuth。小节标题交给
 *   NativeAuthList（heading），读取在发出前被拒绝时标题与清单一起不渲染。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { methodRefusal, type MethodRefusal } from '../../../../client';
import { Badge } from '../../../../components/Badge';
import { useNotifyUnavailable } from '../../../../components/notifications';
import { Button, Dialog, EmptyState, Select } from '../../../../components/primitives';
import {
  AGENT_HARNESS_OPTIONS,
  keyCompatibleWithHarness,
} from '../AgentForm';
import {
  AgentRequestError,
  agentFailureMessage,
  toAgentRequestError,
  type AgentExpected,
  type AgentsApi,
} from '../api';
import type { AgentProviderStatusState } from '../api/use-agent-directory';
import {
  AGENT_GRANT_STATE_LABELS,
  grantTone,
  type AgentCapabilityGrantView,
  type AgentProviderGrantView,
  type ProviderLoginState,
} from '../agent-extensions';
import { SECRET_PROVIDER_ENVIRONMENTS, type CredentialEntry, type SharedCredentialsApi } from '../../../connections';
import { CredentialList, CredentialListItem, NativeAuthList, providerLoginFileLocation, providerLoginFileMessage, type NativeAuthApi, type NativeAuthRecord, type ProviderLoginFile } from '../../../agent-provider';

export interface CredentialsTabProps {
  readonly active?: boolean;
  readonly actor?: string;
  readonly expectedAgent?: AgentExpected | null;
  readonly agentsApi?: AgentsApi;
  readonly providerStatus?: AgentProviderStatusState;
  readonly credentialsApi?: SharedCredentialsApi;
  readonly nativeAuthApi?: NativeAuthApi;
  readonly runtimeRevision?: number;
  readonly nativeLoginFiles?: (
    actor: string,
    provider: string,
  ) => Promise<readonly ProviderLoginFile[]>;
  readonly preferredHarness?: string;
  readonly harness?: string;
  readonly onNavigateConnections: () => void;
  /** agent.secret.provider-list is callable; false = no Key source and no request. */
  readonly keysReadable?: boolean;
  /** Catalog check run before an editor, dialog or busy state starts. */
  readonly checkAccess?: (method: string) => MethodRefusal | null;
  readonly providerGrants?: readonly AgentProviderGrantView[];
  readonly capabilityGrants?: readonly AgentCapabilityGrantView[];
  readonly providerLogins?: Readonly<Record<string, ProviderLoginState>>;
  readonly providerDefault?: { readonly ready: boolean; readonly provider: string | null };
}

type CredentialType = 'key' | 'auth';

type KeysView =
  | { readonly state: 'idle' }
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly entries: readonly CredentialEntry[] }
  | { readonly state: 'error'; readonly message: string };

function errorText(error: unknown): string {
  const failure = error instanceof AgentRequestError
    ? error
    : toAgentRequestError(error);
  return agentFailureMessage(failure).text;
}

function keyLabelOf(
  entries: readonly CredentialEntry[],
  keyId: string | null,
): string {
  if (keyId === null) {
    return '—';
  }
  const entry = entries.find((candidate) => candidate.keyId === keyId);
  return entry ? `${entry.label}（${entry.provider}）` : keyId;
}

function expiresLabel(ms: number | null): string {
  return ms === null ? '—' : new Date(ms).toLocaleString('zh-CN', { hour12: false });
}

function defaultHarness(
  harness: string | undefined,
  preferredHarness: string | undefined,
): string {
  const candidate = preferredHarness || harness || '';
  return AGENT_HARNESS_OPTIONS.includes(
    candidate as (typeof AGENT_HARNESS_OPTIONS)[number],
  )
    ? candidate
    : '';
}

export function CredentialsTab({
  active = true,
  actor = '',
  agentsApi,
  providerStatus = { view: { status: 'idle' }, reload: () => undefined },
  credentialsApi,
  nativeAuthApi,
  runtimeRevision = 0,
  nativeLoginFiles,
  preferredHarness,
  harness,
  onNavigateConnections,
  keysReadable = true,
  checkAccess,
}: CredentialsTabProps) {
  const allowProvider = agentsApi?.allowProvider?.bind(agentsApi);
  const loginNative = agentsApi?.loginNative?.bind(agentsApi);
  const logoutNative = agentsApi?.logoutNative?.bind(agentsApi);
  const nativeAuth = nativeAuthApi ?? credentialsApi?.nativeAuth;
  const keyWritable = allowProvider !== undefined && (checkAccess?.('agent.secret.grant') ?? null) === null;
  const authWritable = loginNative !== undefined && (checkAccess?.('agent.provider.login') ?? null) === null;
  const [keys, setKeys] = useState<KeysView>({ state: 'idle' });
  const notifyUnavailable = useNotifyUnavailable();
  const [authRevision, setAuthRevision] = useState(0);
  const [editOpen, setEditOpen] = useState(false);
  const [credentialType, setCredentialType] = useState<CredentialType>('key');
  const [keyId, setKeyId] = useState('');
  const [keyProvider, setKeyProvider] = useState('');
  const [authHarness, setAuthHarness] = useState(
    defaultHarness(harness, preferredHarness),
  );
  const [authFiles, setAuthFiles] = useState<readonly ProviderLoginFile[] | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [replaceAuth, setReplaceAuth] = useState(false);
  const [existingAuth, setExistingAuth] = useState<NativeAuthRecord | null>(null);
  const [authMode, setAuthMode] = useState<'existing' | 'upload'>('upload');
  const [readingAuth, setReadingAuth] = useState(false);
  const [authReadFailed, setAuthReadFailed] = useState(false);
  const editorEpoch = useRef(0);
  const [pickingAuth, setPickingAuth] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<
    { readonly tone: 'status' | 'error'; readonly text: string } | null
  >(null);

  useEffect(() => {
    setEditOpen(false);
    setExistingAuth(null);
    return () => { editorEpoch.current += 1; };
  }, [actor, credentialsApi]);

  async function loadKeys(): Promise<void> {
    if (!credentialsApi || !keysReadable) {
      setKeys({ state: 'idle' });
      return;
    }
    setKeys({ state: 'loading' });
    try {
      const entries = await credentialsApi.listKeys();
      setKeys({
        state: 'ready',
        entries: entries.filter((entry) => entry.state === 'active'),
      });
    } catch (error) {
      setKeys(
        methodRefusal(error) !== null
          ? { state: 'idle' }
          : { state: 'error', message: 'Key 列表读取失败。' },
      );
    }
  }

  useEffect(() => {
    if (!active) return;
    void loadKeys();
  }, [active, credentialsApi, keysReadable]);

  const keyEntries = keys.state === 'ready' ? keys.entries : [];
  const activeHarness = preferredHarness || harness || '';
  const compatibleKeys = useMemo(
    () => keyEntries.filter((entry) => !entry.provider && !entry.endpoint || keyCompatibleWithHarness(activeHarness, entry.provider, entry.endpoint, entry.protocol)),
    [activeHarness, keyEntries],
  );
  const grants = providerStatus.view.status === 'ready' ? providerStatus.view.rows : [];
  const grantByProvider = new Map(
    grants.filter((grant) => grant.state === 'granted' || grant.state === 'ok').map((grant) => [grant.provider, grant]),
  );

  useEffect(() => {
    if (!editOpen || credentialType !== 'key') return;
    if (compatibleKeys.some((entry) => entry.keyId === keyId)) return;
    setKeyId(compatibleKeys[0]?.keyId ?? '');
  }, [compatibleKeys, credentialType, editOpen, keyId]);

  const selectedKey = compatibleKeys.find(entry => entry.keyId === keyId);
  const selectedProvider = selectedKey?.provider || keyProvider;
  useEffect(() => { setKeyProvider(''); }, [keyId, actor]);

  const keyOffered = keyWritable && compatibleKeys.length > 0;
  const authOffered = authWritable || existingAuth?.state === 'stored';
  const offeredTypes: CredentialType[] = [
    ...(keyOffered ? ['key' as const] : []),
    ...(authOffered ? ['auth' as const] : []),
  ];

  async function openEditor(): Promise<void> {
    const epoch = ++editorEpoch.current;
    const selectedHarness = defaultHarness(harness, preferredHarness);
    const reader = nativeAuth;
    let prefetched: readonly NativeAuthRecord[] | null = null;
    if (!keyOffered && !authWritable) {
      try {
        prefetched = reader ? await reader.list(actor) : null;
      } catch {
        prefetched = null;
      }
      if (epoch !== editorEpoch.current) return;
      const stored = prefetched?.some(
        (row) => row.actor === actor && row.harness === selectedHarness && row.state === 'stored',
      );
      if (!stored) {
        const refusal = checkAccess?.('agent.provider.login') ?? checkAccess?.('agent.secret.grant') ?? null;
        if (refusal) notifyUnavailable('编辑凭据', refusal);
        return;
      }
    }
    setMessage(null);
    setCredentialType(keyOffered ? 'key' : 'auth');
    setKeyId(compatibleKeys[0]?.keyId ?? '');
    setAuthHarness(selectedHarness);
    setAuthFiles(null);
    setAuthError(null);
    setReplaceAuth(false);
    setExistingAuth(null);
    setAuthMode('upload');
    setAuthReadFailed(false);
    setEditOpen(true);
    setReadingAuth(Boolean(reader) && prefetched === null);
    if (!reader) return;
    try {
      const rows = prefetched ?? await reader.list(actor);
      if (epoch !== editorEpoch.current) return;
      const record = rows.find(row => row.actor === actor && row.harness === selectedHarness) ?? null;
      setExistingAuth(record?.state === 'unavailable' ? null : record);
      setAuthReadFailed(record?.state === 'unavailable');
      if (record?.state === 'stored') {
        setAuthMode('existing');
        if (grantByProvider.size === 0 || !keyOffered) setCredentialType('auth');
      }
    } catch {
      if (epoch === editorEpoch.current) setAuthReadFailed(true);
    } finally {
      if (epoch === editorEpoch.current) setReadingAuth(false);
    }
  }

  function closeEditor(): void {
    editorEpoch.current += 1;
    setEditOpen(false);
    setAuthFiles(null);
  }

  async function chooseAuthFiles(): Promise<void> {
    if (nativeLoginFiles === undefined) return;
    setPickingAuth(true);
    setAuthError(null);
    try {
      setAuthFiles(await nativeLoginFiles(actor, authHarness));
    } catch (error) {
      setAuthError(providerLoginFileMessage(error, authHarness));
    } finally {
      setPickingAuth(false);
    }
  }

  async function saveCredential(): Promise<void> {
    if (agentsApi === undefined) return;
    const writeMethod = credentialType === 'key' ? 'agent.secret.grant'
      : authMode === 'existing' && nativeAuth ? null : 'agent.provider.login';
    const refusal = writeMethod === null ? null : checkAccess?.(writeMethod);
    if (refusal) {
      notifyUnavailable('保存凭据', refusal);
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      if (credentialType === 'key') {
        const entry = compatibleKeys.find((candidate) => candidate.keyId === keyId);
        if (entry === undefined) {
          setMessage({ tone: 'error', text: '请选择 Key。' });
          return;
        }
        if (providerStatus.view.status !== 'ready' || !keyCompatibleWithHarness(activeHarness, selectedProvider, selectedKey?.endpoint, selectedKey?.protocol)) {
          setMessage({ tone: 'error', text: '请刷新授权记录并选择模型服务商。' });
          return;
        }
        const matching = grants.filter(grant => grant.provider === selectedProvider
          && (grant.state === 'granted' || grant.state === 'ok'));
        if (matching.length > 1 || matching.some(grant => grant.state === 'granted' && grant.revision === undefined)) {
          setMessage({ tone: 'error', text: '存在多个或未确认的授权，请到凭据设置核对后再修改。' });
          return;
        }
        const previous = matching[0];
        await allowProvider?.(actor, selectedProvider, {
          keyId: entry.keyId, replace: Boolean(previous),
          grantId: previous?.grantId ?? `model-${selectedProvider}`, revision: (previous?.revision ?? 0) + 1,
        });
      } else {
        if (authMode === 'existing' && nativeAuth) {
          const rows = await nativeAuth.list(actor);
          const record = rows.find(row => row.actor === actor && row.harness === authHarness && row.state === 'stored');
          if (!record) {
            setExistingAuth(null);
            if (authWritable) setAuthMode('upload');
            setAuthFiles(null);
            setAuthError(authWritable ? '已有 Auth 已移除，请选择登录文件。' : '已有 Auth 已移除。');
            setAuthRevision(value => value + 1);
            return;
          }
          closeEditor();
          setAuthRevision(value => value + 1);
          return;
        }
        if (authFiles === null || authFiles.length === 0) {
          setMessage({ tone: 'error', text: '请选择登录文件。' });
          return;
        }
        await loginNative?.(actor, authHarness, authFiles, { replace: existingAuth !== null || replaceAuth });
        setAuthRevision(value => value + 1);
      }
      closeEditor();
      providerStatus.reload();
      await loadKeys();
    } catch (error) {
      const refused = methodRefusal(error);
      if (refused) notifyUnavailable('保存凭据', refused);
      else setMessage({ tone: 'error', text: errorText(error) });
    } finally {
      setBusy(false);
    }
  }

  const grantView = providerStatus.view;
  const canEdit = agentsApi !== undefined
    && ((credentialsApi !== undefined && keysReadable && allowProvider !== undefined)
      || (nativeLoginFiles !== undefined && loginNative !== undefined));
  const canSaveType = credentialType === 'key'
    ? credentialsApi !== undefined && selectedKey !== undefined && keyCompatibleWithHarness(activeHarness, selectedProvider, selectedKey?.endpoint, selectedKey?.protocol) && providerStatus.view.status === 'ready'
    : !readingAuth && (authMode === 'existing'
      ? existingAuth !== null && nativeAuth !== undefined
      : nativeLoginFiles !== undefined && loginNative !== undefined
        && authHarness !== '' && Boolean(authFiles?.length));

  return (
    <div className="agent-detail-stack">
      <section className="card agent-detail-card" aria-label="凭据">
        <h3 className="agent-detail-card-title">凭据</h3>

        {grantView.status !== 'unavailable' && <h4>Key</h4>}
        {grantView.status === 'unavailable' ? null
        : grantView.status === 'loading' || grantView.status === 'idle' ? (
          <p className="agent-manage-note" role="status">正在读取 Key 授权…</p>
        ) : grantView.status === 'error' ? (
          <p className="agent-manage-note" role="alert">Key 授权读取失败。</p>
        ) : grants.length === 0 ? (
          <EmptyState title="无 Key 授权。" />
        ) : (
          <div data-testid="agent-key-grants">
            <CredentialList label="Key 授权">
              {grants.map((grant) => (
                <CredentialListItem key={grant.grantId} title={grant.provider || '服务商未指定'}
                  summary={<>Key · <Badge tone={grantTone(grant.state)}>{AGENT_GRANT_STATE_LABELS[grant.state]}</Badge></>}
                  defaultExpanded>
                  <dl className="credentials-facts">
                    <div><dt>Key</dt><dd>{keyLabelOf(keyEntries, grant.keyId)}</dd></div>
                    {grant.expiresAtMs !== null && <div><dt>到期</dt><dd>{expiresLabel(grant.expiresAtMs)}</dd></div>}
                  </dl>
                </CredentialListItem>
              ))}
            </CredentialList>
          </div>
        )}

        <NativeAuthList
          heading="Auth"
          headingLevel={4}
          api={nativeAuth}
          actor={actor}
          revision={authRevision + runtimeRevision}
          disabled={busy || editOpen}
          beforeRemove={() => {
            const refusal = checkAccess?.('agent.provider.logout');
            if (!refusal) return true;
            notifyUnavailable('移除登录文件', refusal);
            return false;
          }}
          onRemove={logoutNative ? async record => {
            setBusy(true);
            try { await logoutNative(record.actor, record.harness); }
            finally { setBusy(false); }
          } : undefined}
        />

        <div className="agent-detail-actions">
          {canEdit && (
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => void openEditor()}
            >
              编辑凭据
            </Button>
          )}
          {keys.state === 'ready' && keyEntries.length === 0 && (
            <Button variant="text" size="compact" onClick={onNavigateConnections}>
              去设置添加 Key
            </Button>
          )}
        </div>
        {message && (
          <p className="agent-form-message" role={message.tone === 'error' ? 'alert' : 'status'}>
            {message.text}
          </p>
        )}
      </section>

      <Dialog
        open={editOpen}
        title="编辑凭据"
        onClose={() => {
          if (!busy && !pickingAuth) closeEditor();
        }}
        actions={[
          {
            label: '取消',
            variant: 'secondary',
            disabled: busy || pickingAuth,
            onClick: closeEditor,
          },
          {
            label: credentialType === 'auth' && authMode === 'existing' ? '完成' : '保存',
            variant: 'primary',
            loading: busy,
            disabled: busy || pickingAuth || !canSaveType,
            onClick: () => void saveCredential(),
          },
        ]}
      >
        <div className="agent-config-form">
        {offeredTypes.length > 1 && <>
        <label htmlFor="agent-credential-type">凭据</label>
        <Select
          id="agent-credential-type"
          value={credentialType}
          disabled={busy || readingAuth}
          onValueChange={(value) => {
            setCredentialType(value as CredentialType);
            setMessage(null);
          }}
          options={offeredTypes.map((type) => ({ value: type, label: type === 'key' ? 'Key' : 'Auth' }))}
        />
        <p className="agent-manage-note" role="status">
          保存不移除另一类凭据。
        </p>
        </>}

        {credentialType === 'key' ? (
          <>
            <label htmlFor="agent-credential-key">Key</label>
            <Select
              id="agent-credential-key"
              value={keyId}
              disabled={busy || keys.state !== 'ready' || compatibleKeys.length === 0}
              onValueChange={setKeyId}
              options={
                compatibleKeys.length === 0
                  ? [{ value: '', label: '没有可用 Key' }]
                  : compatibleKeys.map((entry) => ({
                      value: entry.keyId,
                      label: `${entry.label} · ${entry.provider}`,
                    }))
              }
            />
            {selectedKey?.endpoint && <p className="agent-manage-note">{selectedKey.endpoint} · 启动时请选择 Pi 后台运行的 deepseek-flash。</p>}
            {selectedKey && !selectedKey.provider && <>
              <label htmlFor="agent-credential-key-provider">模型服务商</label>
              <Select id="agent-credential-key-provider" value={keyProvider} onValueChange={setKeyProvider} disabled={busy}
                options={[{ value: '', label: '请选择' }, ...Object.keys(SECRET_PROVIDER_ENVIRONMENTS)
                  .filter(value => keyCompatibleWithHarness(activeHarness, value)).map(value => ({ value, label: value }))]} />
            </>}
            {keys.state === 'loading' && (
              <p className="agent-manage-note" role="status">正在读取 Key…</p>
            )}
            {keys.state === 'error' && (
              <p className="agent-manage-note" role="alert">{keys.message}</p>
            )}
            {keys.state === 'ready' && compatibleKeys.length === 0 && (
              <p className="agent-manage-note" role="status">没有兼容的 Key。</p>
            )}
            {grantByProvider.has(
              compatibleKeys.find((entry) => entry.keyId === keyId)?.provider ?? '',
            ) && (
              <p className="agent-manage-note" role="status">保存后替换当前 Key。</p>
            )}
          </>
        ) : (
          <>
            <label htmlFor="agent-credential-harness">harness</label>
            <input
              className="ui-native-control"
              id="agent-credential-harness"
              value={authHarness}
              readOnly
            />
            {readingAuth ? <p className="agent-manage-note" role="status">正在读取 Auth…</p> : <>
            {authReadFailed && <p className="agent-form-message" role="alert">Auth 读取失败。</p>}
            {existingAuth?.state === 'stored' && authWritable && <>
              <label htmlFor="agent-credential-auth-mode">登录文件</label>
              <Select id="agent-credential-auth-mode" value={authMode} disabled={busy || pickingAuth}
                onValueChange={value => { setAuthMode(value as 'existing' | 'upload'); setAuthError(null); }}
                options={[{ value: 'existing', label: '使用已有 Auth' }, { value: 'upload', label: '上传替换' }]} />
            </>}
            {authMode === 'existing' && existingAuth ? <p className="agent-manage-note">
              {existingAuth.files.join('、')}
            </p> : authWritable && <>
            {providerLoginFileLocation(authHarness) && <p className="agent-field-hint">{providerLoginFileLocation(authHarness)}</p>}
            {(!nativeAuth || authReadFailed) && <label className="agent-credential-replace">
              <input type="checkbox" checked={replaceAuth} disabled={busy}
                onChange={event => setReplaceAuth(event.target.checked)} />
              替换已有 Auth
            </label>}
            {nativeLoginFiles !== undefined && (
              <Button
                type="button"
                variant="secondary"
                disabled={busy || pickingAuth || authHarness === ''}
                onClick={() => void chooseAuthFiles()}
              >
                {pickingAuth ? '读取中…' : authHarness === 'pi' ? '选择两个登录文件' : '选择登录文件'}
              </Button>
            )}
            {authFiles && authFiles.length > 0 && (
              <p className="agent-manage-note" role="status">
                已选择：{authFiles.map((file) => file.filename).join('、')}
              </p>
            )}
            </>}
            </>}
            {authError && (
              <p className="agent-form-message" role="alert">{authError}</p>
            )}
          </>
        )}

        {message && message.tone === 'error' && (
          <p className="agent-form-message" role="alert">{message.text}</p>
        )}
        </div>
      </Dialog>
    </div>
  );
}
