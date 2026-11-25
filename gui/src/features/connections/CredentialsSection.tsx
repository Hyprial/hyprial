/**
 * One shared credential view consumes saved-entry metadata and grant observations
 * independently. Saving/replacing uses the existing Secret writer; revocation
 * targets the exact grant. Missing metadata remains unknown and deletion is
 * hidden when no endpoint exists. Native Auth keeps its independent reader.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { CredentialList, CredentialListItem, NativeAuthList } from '../agent-provider';
import { SECRET_PROVIDER_ENVIRONMENTS, secretEntryReference } from './impl/secret-entries';
import { methodRefusal } from '../../client';
import { useNotifyUnavailable } from '../../components/notifications';
import {
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  ModuleSection,
  Notice,
  Select,
} from '../../components/primitives';
import { SecretField } from '../../components/forms';
import {
  validEndpoint,
  type CredentialProbe,
  type CredentialTestRequest,
  type CredentialEntry,
  type CredentialGrant,
  type SharedCredentialWrite,
  type SharedCredentialsApi,
  type SharedGrantRow,
} from './shared-credentials-api';
import {
  CREDENTIAL_PROVIDERS,
  CREDENTIAL_PROTOCOLS,
  CREDENTIALS_SECTION_COPY,
  type CredentialsSectionModel,
} from './credentials-view-model';

const DEFAULT_PROVIDER = CREDENTIAL_PROVIDERS[0];

type View =
  | { readonly state: 'loading' }
  | { readonly state: 'absent' }
  | { readonly state: 'error' }
  | { readonly state: 'ready' };

type TestResult = CredentialProbe | { readonly kind: 'invalid' } | { readonly kind: 'failed' };

type ShownWrite = Exclude<SharedCredentialWrite, { readonly status: 'unavailable' }>;

interface AuthGroup {
  readonly key: string;
  readonly owner: string;
  readonly entryId: string;
  readonly grants: readonly SharedGrantRow[];
}

function groupAuth(grants: readonly SharedGrantRow[]): readonly AuthGroup[] {
  const groups = new Map<string, AuthGroup>();
  for (const grant of grants) {
    if (grant.source === 'user-provider') continue;
    const owner = grant.source === 'agent-private'
      ? grant.actor
      : grant.ownerActor ?? '未确认';
    const key = `${grant.source}\u0000${owner}\u0000${grant.entryId}\u0000${grant.fieldName ?? ''}`;
    const existing = groups.get(key);
    groups.set(key, {
      key,
      owner,
      entryId: grant.entryId,
      grants: [...(existing?.grants ?? []), grant],
    });
  }
  return [...groups.values()].sort((left, right) => (
    left.owner.localeCompare(right.owner) || left.entryId.localeCompare(right.entryId)
  ));
}

function failureView(error: unknown): View {
  return methodRefusal(error) ? { state: 'absent' } : { state: 'error' };
}

function writeTone(outcome: ShownWrite): 'info' | 'warning' | 'danger' {
  if (outcome.status === 'saved') return 'info';
  return outcome.status === 'failed' ? 'danger' : 'warning';
}

function writeText(outcome: ShownWrite): string {
  if (outcome.status === 'saved') return outcome.message ?? '操作已保存。';
  return outcome.message;
}

function testResultMessage(result: TestResult): string {
  if (result.kind === 'invalid') return '请检查 Key、服务地址和测试模型。';
  if (result.kind === 'failed') return '测试未完成，请重试。';
  const timing = result.simulated ? '（模拟）' : ` · ${result.latencyMs} ms`;
  const outcomes = {
    'authentication-rejected': 'Key 被模型服务拒绝', 'rate-limited': '模型服务限流，请稍后重试',
    'redirect-refused': '服务地址发生重定向，请核对地址', 'request-rejected': '模型服务拒绝请求，请核对模型与协议',
    'upstream-error': '模型服务暂时出错', 'invalid-response': '未收到有效模型回复',
    timeout: '模型服务响应超时', unreachable: '无法连接模型服务',
  };
  if (result.outcome && result.outcome !== 'passed') return `${outcomes[result.outcome]}${timing}`;
  if (result.kind === 'blocked') return `无法连接模型服务${timing}`;
  return result.ok ? `测试通过${timing}` : `测试未通过${result.status ? ` · HTTP ${result.status}` : ''}${timing}`;
}

async function probeKey(
  api: SharedCredentialsApi,
  request: CredentialTestRequest,
): Promise<TestResult | { readonly refused: ReturnType<typeof methodRefusal> }> {
  if (!api.testKey) return { kind: 'failed' };
  if (!request.secret.trim() || !request.model.trim() || (request.endpoint && !validEndpoint(request.endpoint))) return { kind: 'invalid' };
  try {
    return await api.testKey(request);
  } catch (error) {
    const refused = methodRefusal(error);
    return refused ? { refused } : { kind: 'failed' };
  }
}

function nameOf(entry: CredentialEntry): string {
  return entry.label || `${entry.provider} · ${entry.keyId.slice(-6)}`;
}

function agentOf(grant: CredentialGrant): string {
  return grant.agentId ?? grant.agent;
}

function formatTime(value: number | null): string {
  if (value === null) return '—';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

function nameError(value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return '名称不能为空。';
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(trimmed)) return '名称仅支持字母、数字、点、下划线和短横线，最多 128 字符。';
  return undefined;
}

export interface CredentialsSectionProps {
  readonly api?: SharedCredentialsApi;
  readonly onKeysChanged?: () => void;
  readonly creationOnly?: boolean;
  /** Fixture-only display model; live composition uses `api`. */
  readonly model?: CredentialsSectionModel;
}

export function CredentialsSection({ api, model, onKeysChanged, creationOnly = false }: CredentialsSectionProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const generation = useRef(0);
  const probeGeneration = useRef(0);
  const [authRevision, setAuthRevision] = useState(0);
  const [view, setView] = useState<View>(
    api ? { state: 'loading' } : model ? { state: 'ready' } : { state: 'absent' },
  );
  const [entries, setEntries] = useState<readonly CredentialEntry[]>(model?.entries ?? []);
  const [grantView, setGrantView] = useState<View>(api ? { state: 'loading' } : { state: 'ready' });
  const [authGrants, setAuthGrants] = useState<readonly SharedGrantRow[]>(model?.authGrants ?? []);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [providerId, setProviderId] = useState(DEFAULT_PROVIDER.id);
  const [label, setLabel] = useState(DEFAULT_PROVIDER.label);
  const [keyValue, setKeyValue] = useState('');
  const [customAddress, setCustomAddress] = useState(false);
  const [endpoint, setEndpoint] = useState('');
  const [testModel, setTestModel] = useState('');
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [busy, setBusy] = useState<'refresh' | 'add' | 'rotate' | 'delete' | 'revoke' | null>(null);
  const [write, setWrite] = useState<ShownWrite | null>(null);
  const [locked, setLocked] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CredentialEntry | null>(null);
  const [rotateTarget, setRotateTarget] = useState<CredentialEntry | null>(null);
  const [rotateValue, setRotateValue] = useState('');
  const [rotateTestModel, setRotateTestModel] = useState('');
  const [rotateTestResult, setRotateTestResult] = useState<TestResult | null>(null);
  const [rotateTesting, setRotateTesting] = useState(false);
  const rotateKeyRef = useRef<HTMLInputElement>(null);
  const rotateFormRef = useRef<HTMLFormElement>(null);

  const canTest = (provider: string) => Boolean(api?.testKey && (!api.testProviders || api.testProviders.includes(provider)));

  function applyKeys(keys: readonly CredentialEntry[]): void {
    setEntries(keys);
    setView({ state: 'ready' });
  }

  function load(nextApi: SharedCredentialsApi, current: number): void {
    setView({ state: 'loading' });
    void nextApi.listKeys()
      .then(snapshot => {
        if (generation.current !== current) return;
        applyKeys(snapshot);
      })
      .catch((error: unknown) => {
        if (generation.current !== current) return;
        setView(failureView(error));
      });
  }

  useEffect(() => {
    const current = ++generation.current;
    setWrite(null);
    setExpanded(null);
    setFormOpen(false);
    setProviderId(DEFAULT_PROVIDER.id);
    setLabel(DEFAULT_PROVIDER.label);
    setKeyValue('');
    setCustomAddress(false);
    setEndpoint('');
    setTestModel('');
    setTestResult(null);
    setTesting(false);
    setLocked(false);
    setDeleteTarget(null);
    setRotateTarget(null);
    setRotateValue('');
    setRotateTestModel('');
    setRotateTestResult(null);
    setRotateTesting(false);
    if (!api) {
      setEntries(model?.entries ?? []);
      setAuthGrants(model?.authGrants ?? []);
      setView(model ? { state: 'ready' } : { state: 'absent' });
      return;
    }
    load(api, current);
    return () => {
      generation.current += 1;
    };
  }, [api, model]);

  useEffect(() => {
    let disposed = false;
    setAuthGrants(model?.authGrants ?? []);
    setGrantView(api ? { state: 'loading' } : { state: 'ready' });
    if (api && !creationOnly) void api.listSecretGrants().then(grants => {
      if (disposed) return;
      setAuthGrants(grants);
      setGrantView({ state: 'ready' });
    }).catch(error => {
      if (!disposed) setGrantView(failureView(error));
    });
    return () => { disposed = true; };
  }, [api, model, authRevision, creationOnly]);

  async function reload(): Promise<void> {
    if (!api) return;
    const snapshot = await api.listKeys();
    applyKeys(snapshot);
    setAuthRevision(value => value + 1);
    onKeysChanged?.();
  }

  async function refresh(): Promise<void> {
    if (!api || busy) return;
    const previous = view;
    const current = ++generation.current;
    setBusy('refresh');
    setView({ state: 'loading' });
    try {
      const snapshot = await api.listKeys();
      if (generation.current !== current) return;
      setWrite(null);
      setLocked(false);
      setAuthRevision(value => value + 1);
      applyKeys(snapshot);
      onKeysChanged?.();
    } catch (error) {
      if (generation.current !== current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        setView(previous);
        notifyUnavailable(CREDENTIALS_SECTION_COPY.refresh, refusal);
      } else {
        setView({ state: 'error' });
      }
    } finally {
      if (generation.current === current) setBusy(null);
    }
  }

  function permitted(action: 'add' | 'replace' | 'delete' | 'revoke', subject: string): boolean {
    const refusal = api?.refusal?.(action) ?? null;
    if (!refusal) return true;
    notifyUnavailable(subject, refusal);
    return false;
  }

  function openForm(): void {
    if (!permitted('add', CREDENTIALS_SECTION_COPY.add)) return;
    setWrite(null);
    setFormOpen(true);
  }

  function closeForm(): void {
    probeGeneration.current += 1;
    setFormOpen(false);
    setProviderId(DEFAULT_PROVIDER.id);
    setLabel(DEFAULT_PROVIDER.label);
    setKeyValue('');
    setCustomAddress(false);
    setEndpoint('');
    setTestModel('');
    setTestResult(null);
    setTesting(false);
  }

  function selectProvider(id: string): void {
    setCustomAddress(false);
    setEndpoint('');
    setTestModel('');
    const preset = CREDENTIAL_PROVIDERS.find(item => item.id === id) ?? DEFAULT_PROVIDER;
    setProviderId(preset.id);
    setLabel(preset.label.replace(/\s+/g, '-'));
    setTestResult(null);
  }

  async function testKey(): Promise<void> {
    if (!api || !canTest(providerId) || !keyValue || !testModel.trim() || testing) return;
    const current = generation.current;
    const probe = ++probeGeneration.current;
    setTesting(true);
    setTestResult(null);
    const result = await probeKey(api, {
      provider: providerId,
      secret: keyValue,
      model: testModel.trim(),
      ...(customAddress ? { endpoint: endpoint.trim(), protocol: 'openai-completions' } : {}),
    });
    if (generation.current !== current || probeGeneration.current !== probe) return;
    setTesting(false);
    if ('refused' in result) {
      if (result.refused) notifyUnavailable('测试', result.refused);
      return;
    }
    setTestResult(result);
  }

  function openRotate(entry: CredentialEntry): void {
    if (!permitted('replace', CREDENTIALS_SECTION_COPY.replaceKey)) return;
    setRotateTarget(entry);
    setRotateValue('');
    setRotateTestModel('');
    setRotateTestResult(null);
    setRotateTesting(false);
  }

  function closeRotate(): void {
    probeGeneration.current += 1;
    setRotateTarget(null);
    setRotateValue('');
    setRotateTestModel('');
    setRotateTestResult(null);
    setRotateTesting(false);
  }

  async function testRotateKey(): Promise<void> {
    if (!api || !rotateTarget || !canTest(rotateTarget.provider) || !rotateValue || !rotateTestModel.trim() || rotateTesting) return;
    const current = generation.current;
    const probe = ++probeGeneration.current;
    setRotateTesting(true);
    setRotateTestResult(null);
    const result = await probeKey(api, {
      provider: rotateTarget.provider,
      secret: rotateValue,
      model: rotateTestModel.trim(),
      ...(rotateTarget.endpoint ? {
        endpoint: rotateTarget.endpoint,
        protocol: rotateTarget.protocol ?? (rotateTarget.provider === 'anthropic' ? 'anthropic-messages' : 'openai-completions'),
      } : {}),
    });
    if (generation.current !== current || probeGeneration.current !== probe) return;
    setRotateTesting(false);
    if ('refused' in result) {
      if (result.refused) notifyUnavailable('测试', result.refused);
      return;
    }
    setRotateTestResult(result);
  }

  async function saveReplacement(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!api || busy || locked || rotateTesting || !rotateTarget) return;
    const target = rotateTarget;
    const value = rotateValue;
    setBusy('rotate');
    try {
      const outcome = await api.replaceKey(target.keyId, value);
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.save, outcome.refusal);
        return;
      }
      setWrite(outcome);
      closeRotate();
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
      closeRotate();
    } finally {
      setBusy(null);
    }
  }

  async function saveKey(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!api || busy || locked || testing) return;
    if (customAddress && (!api.configureKey || providerId !== 'deepseek' || !validEndpoint(endpoint.trim()))) return;
    const value = keyValue;
    const name = label.trim();
    setKeyValue('');
    setWrite(null);
    setBusy('add');
    try {
      const outcome = customAddress
        ? await api.configureKey!({ provider: providerId, label: name, value, endpoint: endpoint.trim() })
        : await api.addKey(providerId, name, value);
      if (outcome.status === 'unavailable') {
        setKeyValue(value);
        notifyUnavailable(CREDENTIALS_SECTION_COPY.save, outcome.refusal);
        return;
      }
      setWrite(outcome);
      if (outcome.status === 'saved') {
        closeForm();
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
    } finally {
      setBusy(null);
    }
  }

  async function confirmDelete(): Promise<void> {
    if (!api?.deleteKey || busy || locked || !deleteTarget) return;
    const target = deleteTarget;
    setBusy('delete');
    try {
      const outcome = await api.deleteKey(target.keyId);
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.deleteConfirm, outcome.refusal);
        return;
      }
      setDeleteTarget(null);
      setWrite(outcome);
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setDeleteTarget(null);
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
    } finally {
      setBusy(null);
    }
  }

  async function revokeGrant(grant: CredentialGrant): Promise<void> {
    if (!api || busy || locked) return;
    if (!permitted('revoke', CREDENTIALS_SECTION_COPY.revokeGrant)) return;
    setBusy('revoke');
    try {
      const outcome = await api.revokeGrant(grant.agent, grant.grantId);
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.revokeGrant, outcome.refusal);
        return;
      }
      setWrite(outcome);
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
    } finally {
      setBusy(null);
    }
  }

  const authGroups = grantView.state === 'ready' ? groupAuth(authGrants) : [];
  const visibleEntries = view.state === 'ready' ? entries.map(entry => entry.grantsKnown !== false ? entry : ({
    ...entry, grantsKnown: grantView.state === 'ready',
    grants: grantView.state !== 'ready' ? [] : authGrants.filter(grant => grant.source === 'user-provider'
      && grant.fieldName !== null && secretEntryReference(grant.entryId, grant.fieldName) === entry.keyId)
      .map(grant => ({ agent: grant.actor, agentId: null, grantId: grant.grantId, provider: entry.provider, state: 'active' })),
  })) : [];
  const writesDisabled = !api || view.state !== 'ready' || busy !== null || locked;
  const trimmedLabel = label.trim();
  const labelError = nameError(trimmedLabel);
  const total = visibleEntries.length + authGroups.length;
  const writeNotice = write && (
    <Notice tone={writeTone(write)} title={write.status === 'saved' ? '已保存' : write.status === 'failed' ? '操作未完成' : '未确认'}>
      {writeText(write)}
    </Notice>
  );

  if (view.state === 'absent') return null;

  return (
    <ModuleSection
      id="credentials-title"
      className="credentials-section"
      title={CREDENTIALS_SECTION_COPY.title}
      actions={api ? (
        <>
          <Button variant="secondary" disabled={busy !== null || testing || rotateTesting} onClick={() => void refresh()}>
            {CREDENTIALS_SECTION_COPY.refresh}
          </Button>
          <Button
            variant="primary"
            disabled={writesDisabled || formOpen}
            onClick={openForm}
          >
            {CREDENTIALS_SECTION_COPY.add}
          </Button>
        </>
      ) : undefined}
    >
      <Dialog open={formOpen} title="添加 Key" onClose={() => { if (!busy) closeForm(); }}>
        <form className="credentials-form credentials-add" onSubmit={(event) => void saveKey(event)}>
          {writeNotice}
          <Field label={CREDENTIALS_SECTION_COPY.provider} htmlFor="credentials-provider">
          <Select
            id="credentials-provider"
            value={providerId}
            options={CREDENTIAL_PROVIDERS.filter(item => SECRET_PROVIDER_ENVIRONMENTS[item.id]).map(item => ({ value: item.id, label: item.label }))}
            onValueChange={selectProvider}
            disabled={writesDisabled || testing}
          />
          </Field>
          {api?.configureKey && providerId === 'deepseek' && <>
            <Field label="服务地址来源" htmlFor="credentials-address-mode">
              <Select id="credentials-address-mode" value={customAddress ? 'custom' : 'default'}
                disabled={writesDisabled || testing}
                onValueChange={value => { setCustomAddress(value === 'custom'); setTestResult(null); }}
                options={[{ value: 'default', label: '默认地址' }, { value: 'custom', label: '自定义地址' }]} />
            </Field>
            {customAddress && <Field label="服务地址" htmlFor="credentials-endpoint"
              hint="仅支持 Pi 后台运行的 deepseek-flash，使用 OpenAI Chat Completions 协议。"
              error={endpoint && !validEndpoint(endpoint.trim()) ? '请输入 HTTPS 地址；本机测试可使用 HTTP。' : undefined}>
              <input id="credentials-endpoint" value={endpoint} placeholder="填写服务提供商给出的地址"
                autoComplete="off" disabled={writesDisabled || testing}
                onChange={event => { setEndpoint(event.target.value); setTestResult(null); }} />
            </Field>}
          </>}
          <Field
            label={CREDENTIALS_SECTION_COPY.name}
            htmlFor="credentials-name"
            hint={CREDENTIALS_SECTION_COPY.nameHint}
            error={labelError}
          >
            <input
              id="credentials-name"
              value={label}
              placeholder="例如 DeepSeek-work"
            disabled={writesDisabled || testing}
              onChange={(event) => setLabel(event.target.value)}
            />
          </Field>
          <SecretField
            layout="inline"
            id="credentials-key"
            label={CREDENTIALS_SECTION_COPY.keyValue}
            value={keyValue}
            onChange={value => { setKeyValue(value); setTestResult(null); }}
            disabled={writesDisabled || testing}
          />
          {canTest(providerId) && (
            <Field label="测试模型" htmlFor="credentials-test-model" hint="测试时必填，不保存。">
              <input id="credentials-test-model" value={testModel} autoComplete="off"
                disabled={writesDisabled || testing}
                onChange={event => { setTestModel(event.target.value); setTestResult(null); }} />
            </Field>
          )}
          {testResult && (
            <p className="credentials-test-result" role="status">
              {testResultMessage(testResult)}
            </p>
          )}
          <div className="credentials-form-actions">
            {canTest(providerId) && (
              <Button
                type="button"
                variant="secondary"
                disabled={writesDisabled || testing || !keyValue || !testModel.trim() || (customAddress && !validEndpoint(endpoint.trim()))}
                loading={testing}
                onClick={() => void testKey()}
              >
                {testing ? '测试中…' : '测试'}
              </Button>
            )}
            <Button type="button" variant="secondary" disabled={busy !== null} onClick={closeForm}>
              {CREDENTIALS_SECTION_COPY.cancel}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={writesDisabled || testing || Boolean(labelError) || !keyValue || (customAddress && !validEndpoint(endpoint.trim()))}
            >
              {CREDENTIALS_SECTION_COPY.save}
            </Button>
          </div>
        </form>
      </Dialog>

      {!formOpen && writeNotice}

      {view.state === 'loading' && <LoadingState title="正在读取凭据" />}
      {view.state === 'error' && <ErrorState message="凭据读取失败，请刷新后重试。" />}
      {!creationOnly && <>
      {grantView.state === 'loading' && <LoadingState title="正在读取服务密钥授权" />}
      {grantView.state === 'error' && <ErrorState message="服务密钥授权读取失败，请刷新后重试。" />}
      {view.state === 'ready' && total === 0 && <EmptyState title={CREDENTIALS_SECTION_COPY.empty} />}
      {total > 0 && (
        <CredentialList label="凭据列表">
          {visibleEntries.map(entry => {
            const key = `key-${entry.keyId}`;
            const open = expanded === key;
            return (
              <CredentialListItem key={key} title={nameOf(entry)}
                summary={<>{CREDENTIALS_SECTION_COPY.keyBadge} · {entry.provider || '服务商未指定'} · {entry.grantsKnown === false ? '授权状态未确认' : `${entry.grants.length} 条授权`}</>}
                expanded={open} onExpandedChange={value => setExpanded(value ? key : null)}>
                    <dl className="credentials-facts">
                      <div>
                        <dt>服务商</dt>
                        <dd>{entry.provider || '未指定，授权时选择'}</dd>
                      </div>
                      {entry.endpoint && <div><dt>服务地址</dt><dd>{entry.endpoint}</dd></div>}
                      {entry.protocol && <div><dt>兼容协议</dt><dd>{CREDENTIAL_PROTOCOLS.find(item => item.value === entry.protocol)?.label}</dd></div>}
                      <div>
                        <dt>状态</dt>
                        <dd>{entry.state === 'active' ? '已保存' : entry.state === 'deleted' ? '已删除' : '状态未确认'}</dd>
                      </div>
                      <div>
                        <dt>创建时间</dt>
                        <dd>{formatTime(entry.createdAt)}</dd>
                      </div>
                      <div>
                        <dt>最近使用</dt>
                        <dd>{formatTime(entry.lastUsedAt)}</dd>
                      </div>
                    </dl>
                    <p className="credentials-row__meta">已授权 Agent</p>
                    {entry.grantsKnown === false ? <p className="credentials-row__meta">授权状态未确认。</p> : entry.grants.length === 0 ? (
                      <p className="credentials-row__meta">尚未授权给任何 Agent。</p>
                    ) : (
                      <ul className="credentials-grant-list" aria-label="授权记录">
                        {entry.grants.map(grant => (
                          <li key={`${grant.grantId}:${grant.agent}`}>
                            <span>{agentOf(grant)}</span>
                            {api && (
                              <Button
                                variant="secondary"
                                size="sm"
                                disabled={busy !== null || locked}
                                aria-label={`撤销 ${agentOf(grant)} 的授权`}
                                onClick={() => void revokeGrant(grant)}
                              >
                                {CREDENTIALS_SECTION_COPY.revokeGrant}
                              </Button>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                    {api && (
                      <div className="credentials-actions">
                        <Button
                          variant="secondary"
                          disabled={busy !== null || locked}
                          onClick={() => openRotate(entry)}
                        >
                          {CREDENTIALS_SECTION_COPY.replaceKey}
                        </Button>
                        {api.deleteKey && <Button
                          variant="danger"
                          disabled={busy !== null || locked}
                          onClick={() => {
                            if (permitted('delete', CREDENTIALS_SECTION_COPY.deleteCredential)) setDeleteTarget(entry);
                          }}
                        >
                          {CREDENTIALS_SECTION_COPY.deleteCredential}
                        </Button>}
                      </div>
                    )}
              </CredentialListItem>
            );
          })}
          {authGroups.map(group => {
            const key = `auth-${group.key}`;
            const open = expanded === key;
            return (
              <CredentialListItem key={key} title={group.entryId}
                summary={<>{CREDENTIALS_SECTION_COPY.authBadge} · 所有者 {group.owner} · {group.grants.length} 个 Agent 已授权</>}
                expanded={open} onExpandedChange={value => setExpanded(value ? key : null)}>
                    <dl className="credentials-facts">
                      <div>
                        <dt>所有者</dt>
                        <dd>{group.owner}</dd>
                      </div>
                    </dl>
                    <p className="credentials-row__meta">使用该凭据的 Agent（只读）</p>
                    <ul className="credentials-grant-list" aria-label="授权记录">
                      {group.grants.map(grant => (
                        <li key={`${grant.grantId}:${grant.actor}`}>
                          <span>{grant.actor}</span>
                        </li>
                      ))}
                    </ul>
                    <p className="credentials-row__meta">{CREDENTIALS_SECTION_COPY.authManaged}</p>
              </CredentialListItem>
            );
          })}
        </CredentialList>
      )}

      <NativeAuthList heading="Auth" api={api?.nativeAuth} revision={authRevision} />
      </>}

      <Dialog
        open={rotateTarget !== null}
        title={CREDENTIALS_SECTION_COPY.replaceTitle}
        description={CREDENTIALS_SECTION_COPY.replaceDescription}
        initialFocusRef={rotateKeyRef}
        onClose={() => {
          if (busy !== 'rotate') closeRotate();
        }}
        actions={[
          ...(rotateTarget && canTest(rotateTarget.provider) ? [{
            label: '测试',
            variant: 'secondary' as const,
            loading: rotateTesting,
            disabled: writesDisabled || rotateTesting || !rotateValue || !rotateTestModel.trim(),
            onClick: () => void testRotateKey(),
          }] : []),
          {
            label: CREDENTIALS_SECTION_COPY.cancel,
            variant: 'secondary',
            disabled: busy === 'rotate',
            onClick: closeRotate,
          },
          {
            label: CREDENTIALS_SECTION_COPY.save,
            variant: 'primary',
            loading: busy === 'rotate',
            disabled: writesDisabled || rotateTesting || !rotateValue,
            onClick: () => rotateFormRef.current?.requestSubmit(),
          },
        ]}
      >
        {rotateTarget && (
          <form className="credentials-form" ref={rotateFormRef} onSubmit={(event) => void saveReplacement(event)}>
            <p className="credentials-row__meta">
              {nameOf(rotateTarget)} · {rotateTarget.provider}
            </p>
            <SecretField
              layout="inline"
              id="credentials-rotate-key"
              label="新 Key"
              value={rotateValue}
              onChange={value => { setRotateValue(value); setRotateTestResult(null); }}
              inputRef={rotateKeyRef}
              disabled={writesDisabled || rotateTesting}
            />
            {canTest(rotateTarget.provider) && (
              <Field label="测试模型" htmlFor="credentials-rotate-model" hint="测试时必填，不保存。">
                <input id="credentials-rotate-model" value={rotateTestModel} autoComplete="off"
                  disabled={writesDisabled || rotateTesting}
                  onChange={event => { setRotateTestModel(event.target.value); setRotateTestResult(null); }} />
              </Field>
            )}
            {rotateTestResult && (
              <p className="credentials-test-result" role="status">
                {testResultMessage(rotateTestResult)}
              </p>
            )}
            <p className="credentials-row__meta">{CREDENTIALS_SECTION_COPY.replaceHint}</p>
          </form>
        )}
      </Dialog>

      <Dialog
        open={deleteTarget !== null}
        title={CREDENTIALS_SECTION_COPY.deleteConfirmTitle}
        description={CREDENTIALS_SECTION_COPY.deleteConfirmDescription}
        onClose={() => {
          if (busy !== 'delete') setDeleteTarget(null);
        }}
        actions={[
          {
            label: CREDENTIALS_SECTION_COPY.cancel,
            variant: 'secondary',
            onClick: () => setDeleteTarget(null),
          },
          {
            label: CREDENTIALS_SECTION_COPY.deleteConfirm,
            variant: 'danger',
            loading: busy === 'delete',
            onClick: () => void confirmDelete(),
          },
        ]}
      >
        {deleteTarget && <p>{nameOf(deleteTarget)}</p>}
      </Dialog>
    </ModuleSection>
  );
}
