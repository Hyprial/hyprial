/**
 * 决策记录 2026-11-10（Agent 创建/编辑表单的两种数据路径）。
 *
 * - 目录（live）路径只写真实契约字段：创建必填名称、harness 和凭据；已知字段由 Key 确定服务商，
 *   未知字段需用户显式选择服务商。目录路径没有编辑表单（改名在详情
 *   页内联完成，只调用 agent.rename）。创建时不选择组织，不把职责写进 draft。
 * - live 创建只提供 Key / Auth，不再有“稍后配置”。Key 下拉来自 `agent.secret.provider-list`；
 *   Auth 没有本机登录枚举接口，只能让用户在提交前选择登录文件，创建成功后用
 *   `agent.provider.login` 写入。共享 Auth 的 GUI 授权方法尚未接线，因此不放进表单。
 * - 模型不在创建表单里填写；创建仅写记录和凭据，再显式配置并启动。
 *   前端预检已知参数约束，daemon 的启动错误是权威结论。
 * - 2026-11-17：自定义地址仅开放明确的 Pi / deepseek / deepseek-flash 组合；
 *   地址和协议随 Key 清单读取，后台启动仍由 daemon 校验。
 * - 2026-11-18（first-run P2）：live 创建增加模型选择；默认保留服务商默认（null），
 *   已有 DeepSeek Key 可选 deepseek-flash，带地址的固定路由只允许 Pi / deepseek-flash。
 *   选项仍是前端预检，daemon 启动校验是权威结论。
 * - 2026-11-18（P3）：Pi + DeepSeek 的普通 Key 同样显式使用 deepseek-flash；
 *   Pi 启动要求 provider/model 成对，不能显示默认却提交 null model。
 * - 2026-11-14：凭据字段只提供有端口的选项——Key 需要 Key 清单端口，Auth 需要登录文件
 *   选择器；两者都没有时不渲染凭据字段，提交空凭据计划。Key 清单读取在发出前被拒绝
 *   （methodRefusal）等同于没有清单端口；真实读取失败仍由告警行显示。
 * - 2026-11-14（验证修订）：Key 来源与 Auth 来源由页面按方法目录交出——Key 需要
 *   agent.secret.provider-list 与 agent.secret.grant 都可调用，Auth 需要 agent.provider.login
 *   可调用；被拒绝的那一类不交出端口，不渲染该选项，两类都没有则不渲染凭据字段。
 *   Key 清单读取失败（已发出）只显示固定文案，不渲染错误对象的 message。
 * - fixture 路径保留 dev-02/dev-04 的既有编辑表单语义（职责、所属组织、服务商、模型、
 *   默认 harness 与“保存修改”），只服务隔离演示，不作为后端能力证据。
 */

import { Fields, choiceError, choiceConfiguration, type ModelChoice } from './model-configuration/Fields';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { methodRefusal } from '../../../client';
import type { AgentOrganization, ManagedAgentView } from '../../../state/agent-selectors';
import type { AgentCredentialPlan, AgentDraft } from './AgentMutationGateway';
import { SECRET_PROVIDER_ENVIRONMENTS, type CredentialEntry, type SharedCredentialsApi } from '../../connections';
import { providerLoginFileNames, providerLoginFileLocation, providerLoginFileMessage, type ProviderLoginFile } from '../../agent-provider';
import { AgentAvatar } from '../../../components/AgentAvatar';
import { Button, Card, InfoTip, Select } from '../../../components/primitives';

export interface AgentFormProps {
  readonly explicitModel?: boolean;
  readonly mode: 'create' | 'edit';
  readonly initial: AgentDraft;
  readonly organizations?: readonly AgentOrganization[];
  /** fixture 路径保留职责字段；创建路径不选择组织。 */
  readonly previewMode?: boolean;
  /** Key 清单；缺席时不提供 Key 选项，不发请求。 */
  readonly credentialsApi?: SharedCredentialsApi;
  readonly credentialsRevision?: number;
  /** 创建前选择 Auth 文件；缺席时不提供 Auth 选项。 */
  readonly pickLoginFiles?: (provider: string) => Promise<readonly ProviderLoginFile[]>;
  readonly pending: boolean;
  /** First-run keeps focus on the step heading until the user tabs into the form. */
  readonly autoFocusName?: boolean;
  /** An unknown write outcome blocks a repeat submission until the user rechecks. */
  readonly submitDisabled?: boolean;
  readonly submitDisabledReason?: string;
  readonly message: { readonly tone: 'error' | 'status'; readonly text: string } | null;
  readonly onSubmit: (draft: AgentDraft) => void;
  readonly onCancel: () => void;
}

const NAME_REQUIRED = '请填写 Agent 名称';
const ROLE_REQUIRED = '请填写 Agent 职责';
const HARNESS_REQUIRED = '请选择 harness';
const KEY_REQUIRED = '请选择 Key';

export function agentNameError(name: string): string | null {
  if (!name) return NAME_REQUIRED;
  return name.length <= 128 && /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)
    ? null : '名称以字母或数字开头，仅支持字母、数字、点、下划线和短横线，最多 128 字符。';
}

export const AGENT_HARNESS_OPTIONS = ['claude', 'codex', 'pi', 'agy'] as const;

const AGENT_HARNESS_LABELS: Readonly<Record<(typeof AGENT_HARNESS_OPTIONS)[number], string>> = {
  claude: 'claude',
  codex: 'codex',
  pi: 'pi',
  agy: 'Antigravity',
};

const KEY_PROVIDERS_BY_HARNESS: Readonly<Record<string, readonly string[]>> = {
  claude: ['anthropic', 'deepseek', 'hyprial'],
  codex: ['deepseek', 'hyprial'],
  pi: ['openai', 'anthropic', 'deepseek', 'kimi-coding', 'moonshotai'],
  agy: ['google'],
};

type CredentialType = 'key' | 'auth';

type KeysView =
  | { readonly state: 'none' }
  | { readonly state: 'idle' }
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly entries: readonly CredentialEntry[] }
  | { readonly state: 'error'; readonly message: string };

export function keyCompatibleWithHarness(harness: string, provider: string, endpoint?: string, protocol?: string): boolean {
  if (endpoint) return harness === 'pi' && provider === 'deepseek' && protocol === 'openai-completions';
  const allowed = KEY_PROVIDERS_BY_HARNESS[harness];
  return allowed?.includes(provider) === true;
}


export function AgentForm({
  explicitModel = false,
  mode,
  initial,
  organizations = [],
  previewMode = false,
  credentialsApi,
  credentialsRevision = 0,
  pickLoginFiles,
  pending,
  autoFocusName = true,
  submitDisabled = false,
  submitDisabledReason,
  message,
  onSubmit,
  onCancel,
}: AgentFormProps) {
  const [choice, setChoice] = useState<ModelChoice>({ harness: initial.preferredHarness ?? '', authKind: 'connection', model: initial.model ?? '' });
  const [explicitError, setExplicitError] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState(initial.displayName);
  const [role, setRole] = useState(initial.role);
  const [organizationId, setOrganizationId] = useState(initial.organizationId ?? '');
  const selectedOrgId = organizationId;
  const [provider, setProvider] = useState(initial.provider ?? '');
  const [model, setModel] = useState(initial.model ?? '');
  const [cwd, setCwd] = useState(initial.cwd ?? '');
  const [preferredHarness, setPreferredHarness] = useState(
    initial.preferredHarness ?? '',
  );
  const [chosenType, setCredentialType] = useState<CredentialType>('key');
  const [keyId, setKeyId] = useState('');
  const [keyProvider, setKeyProvider] = useState('');
  const [authFiles, setAuthFiles] = useState<readonly ProviderLoginFile[] | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [pickingAuth, setPickingAuth] = useState(false);
  const [keys, setKeys] = useState<KeysView>(
    credentialsApi === undefined ? { state: 'none' } : { state: 'idle' },
  );
  const [nameError, setNameError] = useState<string | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [harnessError, setHarnessError] = useState<string | null>(null);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const roleRef = useRef<HTMLInputElement>(null);
  const harnessRef = useRef<HTMLButtonElement>(null);
  const keyRef = useRef<HTMLButtonElement>(null);
  const modelRef = useRef<HTMLButtonElement>(null);
  const authRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (autoFocusName) nameRef.current?.focus();
  }, [autoFocusName]);

  useEffect(() => {
    if (explicitModel || previewMode || mode !== 'create' || credentialsApi === undefined) {
      setKeys({ state: 'none' });
      return;
    }
    let cancelled = false;
    setKeys({ state: 'loading' });
    credentialsApi
      .listKeys()
      .then((entries) => {
        if (cancelled) return;
        setKeys({
          state: 'ready',
          entries: entries.filter((entry) => entry.state === 'active'),
        });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setKeys(
          methodRefusal(error) !== null
            ? { state: 'none' }
            : { state: 'error', message: 'Key 列表读取失败。' },
        );
      });
    return () => {
      cancelled = true;
    };
  }, [credentialsApi, credentialsRevision, mode, previewMode, explicitModel]);

  const compatibleKeys = keys.state === 'ready'
    ? keys.entries.filter((entry) => !entry.provider && !entry.endpoint || keyCompatibleWithHarness(preferredHarness, entry.provider, entry.endpoint, entry.protocol))
    : [];
  const selectedKey = compatibleKeys.find((entry) => entry.keyId === keyId) ?? null;
  const selectedProvider = selectedKey?.provider || keyProvider;
  const keyOffered = keys.state !== 'none';
  const authOffered = pickLoginFiles !== undefined;
  const credentialType: CredentialType | 'none' = !keyOffered && !authOffered ? 'none'
    : chosenType === 'key' && !keyOffered ? 'auth'
      : chosenType === 'auth' && !authOffered ? 'key'
        : chosenType;
  const liveModelOptions = credentialType === 'key' && selectedKey?.endpoint
    ? [{ value: 'deepseek-flash', label: 'deepseek-flash（固定）' }]
    : credentialType === 'key' && selectedKey && selectedProvider === 'deepseek' && preferredHarness === 'pi'
      ? [{ value: 'deepseek-flash', label: 'deepseek-flash（默认）' }]
      : [{ value: '', label: '服务商默认' }];
  const liveModelSupported = liveModelOptions.some((option) => option.value === model.trim());
  const keyChoiceAvailable = keys.state === 'idle' || keys.state === 'loading' || compatibleKeys.length > 0;
  const keyChoiceLabel = keyChoiceAvailable || keys.state === 'error' ? 'Key' : 'Key（暂无适用 Key）';
  const credentialOptions = [
    ...(keyOffered ? [{ value: 'key', label: keyChoiceLabel, disabled: !keyChoiceAvailable }] : []),
    ...(authOffered ? [{ value: 'auth', label: 'Auth' }] : []),
  ];

  useEffect(() => {
    if (preferredHarness && keyOffered && !keyChoiceAvailable && authOffered) setCredentialType('auth');
  }, [preferredHarness, keyOffered, keyChoiceAvailable, authOffered]);

  useEffect(() => {
    if (credentialType !== 'key') return;
    if (selectedKey !== null) return;
    setKeyId(compatibleKeys[0]?.keyId ?? '');
  }, [compatibleKeys, credentialType, selectedKey]);

  useEffect(() => { setKeyProvider(''); }, [keyId, preferredHarness]);

  useEffect(() => {
    if (previewMode || mode !== 'create') return;
    setModel(credentialType === 'key' && (selectedKey?.endpoint || selectedKey && selectedProvider === 'deepseek' && preferredHarness === 'pi')
      ? 'deepseek-flash' : '');
  }, [selectedKey, selectedProvider, preferredHarness, credentialType, previewMode, mode]);

  useEffect(() => {
    setAuthFiles(null);
    setAuthError(null);
  }, [preferredHarness]);

  async function chooseAuthFiles(): Promise<void> {
    if (pickLoginFiles === undefined || preferredHarness === '') {
      setAuthError('请选择 harness');
      return;
    }
    setPickingAuth(true);
    setAuthError(null);
    try {
      const files = await pickLoginFiles(preferredHarness);
      setAuthFiles(files);
    } catch (error) {
      setAuthError(providerLoginFileMessage(error, preferredHarness));
    } finally {
      setPickingAuth(false);
    }
  }

  const title = mode === 'create' ? '创建 Agent' : '编辑 Agent';

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (pending || submitDisabled || pickingAuth) {
      return;
    }
    if (explicitModel && !previewMode && mode === 'create') {
      const invalid = agentNameError(displayName.trim()) ?? choiceError(choice, true);
      setExplicitError(invalid);
      if (invalid) return;
      const common = { displayName: displayName.trim(), role: '', organizationId: null, modelServiceId: initial.modelServiceId,
        preferredHarness: choice.harness, model: choice.model, cwd: cwd.trim() || null };
      onSubmit(choice.authKind === 'connection'
        ? { ...common, modelConfiguration: { configuration: choiceConfiguration(choice), operationId: crypto.randomUUID() } }
        : { ...common, requireModelConfiguration: true });
      return;
    }
    const name = displayName.trim();
    const roleText = role.trim();
    let firstInvalid: HTMLElement | null = null;
    const invalidName = previewMode ? (!name ? NAME_REQUIRED : null) : agentNameError(name);
    if (invalidName) {
      setNameError(invalidName);
      firstInvalid = nameRef.current;
    } else {
      setNameError(null);
    }
    if (previewMode && !roleText) {
      setRoleError(ROLE_REQUIRED);
      firstInvalid = firstInvalid ?? roleRef.current;
    } else {
      setRoleError(null);
    }
    if (!previewMode && mode === 'create' && preferredHarness === '') {
      setHarnessError(HARNESS_REQUIRED);
      firstInvalid = firstInvalid ?? harnessRef.current;
    } else {
      setHarnessError(null);
    }
    if (!previewMode && mode === 'create' && !liveModelSupported) {
      setModelError('请选择受支持的模型');
      firstInvalid = firstInvalid ?? modelRef.current;
    } else {
      setModelError(null);
    }
    let credential: AgentCredentialPlan | undefined;
    if (!previewMode && mode === 'create' && credentialType === 'none') {
      credential = { kind: 'none' };
    } else if (!previewMode && mode === 'create') {
      if (credentialType === 'key' && (selectedKey === null || !keyCompatibleWithHarness(preferredHarness, selectedProvider, selectedKey.endpoint, selectedKey.protocol))) {
        setKeyError(KEY_REQUIRED);
        firstInvalid = firstInvalid ?? keyRef.current;
      } else {
        setKeyError(null);
      }
      if (credentialType === 'auth' && (authFiles === null || authFiles.length === 0)) {
        setAuthError('请选择登录文件');
        firstInvalid = firstInvalid ?? authRef.current;
      } else if (credentialType === 'auth') {
        setAuthError(null);
      }
      credential = credentialType === 'key' && selectedKey !== null
        ? { kind: 'key', keyId: selectedKey.keyId, provider: selectedProvider }
        : { kind: 'auth', harness: preferredHarness, files: authFiles ?? [] };
    }
    if (firstInvalid) {
      firstInvalid.focus();
      return;
    }
    const liveProvider = mode === 'edit'
      ? initial.provider ?? null
      : credential?.kind === 'key'
        ? credential.provider
        : null;
    onSubmit({
      displayName: name,
      role: previewMode ? roleText : initial.role,
      modelServiceId: previewMode ? '' : initial.modelServiceId,
      organizationId: mode === 'create' ? null
        : previewMode ? selectedOrgId || null : initial.organizationId,
      provider: previewMode ? (provider.trim() === '' ? null : provider.trim()) : liveProvider,
      model: previewMode
        ? (model.trim() === '' ? null : model.trim())
        : mode === 'edit'
          ? initial.model ?? null
          : (model.trim() === '' ? null : model.trim()),
      preferredHarness: preferredHarness === '' ? null : preferredHarness,
      ...(!previewMode && mode === 'create' ? { cwd: cwd.trim() || null } : {}),
      ...(credential ? { credential } : {}),
    });
  }

  return (
    <Card
      as="section"
      className="card agent-panel"
      aria-labelledby="agent-form-title"
    >
      <div className="section-heading">
        <div className="agent-form-title-row">
          <h2 id="agent-form-title" tabIndex={-1}>
            {title}
          </h2>
        </div>
      </div>

      <form className="agent-form" onSubmit={handleSubmit} noValidate>
        <label htmlFor="agent-name">名称</label>
        <input
              className="ui-native-control"
          id="agent-name"
          ref={nameRef}
          autoComplete="off"
          value={displayName}
          disabled={pending}
          aria-invalid={nameError ? true : undefined}
          aria-describedby={nameError ? 'agent-name-error' : undefined}
          onChange={(event) => setDisplayName(event.target.value)}
        />
        {nameError && (
          <p className="agent-field-error" id="agent-name-error" role="alert">
            {nameError}
          </p>
        )}

        {!previewMode && mode === 'create' && <>
          <label htmlFor="agent-cwd">工作目录</label>
          <input id="agent-cwd" className="ui-native-control" value={cwd}
            placeholder="留空自动创建 workspace" autoComplete="off" disabled={pending}
            onChange={event => setCwd(event.target.value)} />
        </>}

        {previewMode && mode === 'edit' && <>
          <label htmlFor="agent-organization">所属组织</label>
          <Select
            id="agent-organization"
            value={selectedOrgId}
            disabled={pending}
            onValueChange={setOrganizationId}
            options={[
              { value: '', label: '不加入组织' },
              ...organizations.map((organization) => ({
                value: organization.id,
                label: organization.label,
              })),
            ]}
          />
        </>}

        {previewMode ? (
          <>
            <label htmlFor="agent-role">职责</label>
            <input
              className="ui-native-control"
              id="agent-role"
              ref={roleRef}
              autoComplete="off"
              value={role}
              disabled={pending}
              aria-invalid={roleError ? true : undefined}
              aria-describedby={roleError ? 'agent-role-error' : undefined}
              onChange={(event) => setRole(event.target.value)}
            />
            {roleError && (
              <p className="agent-field-error" id="agent-role-error" role="alert">
                {roleError}
              </p>
            )}

            <label htmlFor="agent-provider">服务商</label>
            <input
              className="ui-native-control"
              id="agent-provider"
              autoComplete="off"
              value={provider}
              disabled={pending}
              placeholder="留空使用账号默认服务商"
              onChange={(event) => setProvider(event.target.value)}
            />

            <label htmlFor="agent-model">模型</label>
            <input
              className="ui-native-control"
              id="agent-model"
              autoComplete="off"
              value={model}
              disabled={pending}
              placeholder="留空使用服务商默认模型"
              onChange={(event) => setModel(event.target.value)}
            />

            <label htmlFor="agent-harness">默认 harness</label>
            <Select
              id="agent-harness"
              value={preferredHarness}
              disabled={pending}
              onValueChange={setPreferredHarness}
              options={[
                { value: '', label: '未指定' },
                ...AGENT_HARNESS_OPTIONS.map((harness) => ({
                  value: harness,
                  label: AGENT_HARNESS_LABELS[harness],
                })),
              ]}
            />
          </>
        ) : explicitModel && mode === 'create' ? <>
          <Fields value={choice} onChange={setChoice} credentials={credentialsApi} revision={credentialsRevision} disabled={pending || submitDisabled} creating />
          {explicitError && <p className="agent-field-error" role="alert">{explicitError}</p>}
        </> : (
          <>
          <p className="agent-field-hint">当前入口采用旧版配置。</p>
            <label htmlFor="agent-harness">harness</label>
            <Select
              id="agent-harness"
              ref={harnessRef}
              value={preferredHarness}
              disabled={pending || pickingAuth}
              onValueChange={setPreferredHarness}
              options={[
                { value: '', label: '请选择' },
                ...AGENT_HARNESS_OPTIONS.map((harness) => ({
                  value: harness,
                  label: AGENT_HARNESS_LABELS[harness],
                })),
              ]}
            />
            {harnessError && (
              <p className="agent-field-error" id="agent-harness-error" role="alert">
                {harnessError}
              </p>
            )}

            <label htmlFor="agent-model">模型</label>
            <Select
              id="agent-model"
              ref={modelRef}
              value={model}
              disabled={pending || pickingAuth}
              onValueChange={(value) => {
                setModel(value);
                setModelError(null);
              }}
              options={liveModelOptions}
            />
            {modelError && (
              <p className="agent-field-error" id="agent-model-error" role="alert">
                {modelError}
              </p>
            )}
            <p className="agent-field-hint">
              Pi · DeepSeek 使用 deepseek-flash；其他组合使用服务商默认模型。
            </p>

            {credentialOptions.length > 0 && <>
            <label htmlFor="agent-credential">凭据</label>
            <Select
              id="agent-credential"
              value={credentialType}
              disabled={pending || pickingAuth}
              onValueChange={(value) => setCredentialType(value as CredentialType)}
              options={credentialOptions}
            />
            </>}

            {credentialType === 'key' && (
              <>
                <label htmlFor="agent-key">Key</label>
                <Select
                  id="agent-key"
                  ref={keyRef}
                  value={keyId}
                  disabled={pending || keys.state !== 'ready' || compatibleKeys.length === 0}
                  onValueChange={setKeyId}
                  options={
                    compatibleKeys.length === 0
                      ? [{ value: '', label: '没有可用 Key' }]
                      : compatibleKeys.map((entry) => ({
                          value: entry.keyId,
                          label: `${entry.label} · ${entry.provider || '选择服务商'}`,
                        }))
                  }
                />
                {selectedKey?.endpoint && <p className="agent-field-hint">{selectedKey.endpoint} · 仅支持 Pi 后台运行的 deepseek-flash，创建后请核对启动配置。</p>}
                {selectedKey && !selectedKey.provider && <>
                  <label htmlFor="agent-key-provider">模型服务商</label>
                  <Select id="agent-key-provider" value={keyProvider} onValueChange={setKeyProvider} disabled={pending}
                    options={[{ value: '', label: '请选择' }, ...Object.keys(SECRET_PROVIDER_ENVIRONMENTS)
                      .filter(value => keyCompatibleWithHarness(preferredHarness, value)).map(value => ({ value, label: value }))]} />
                </>}
                {keys.state === 'loading' && (
                  <p className="agent-field-hint">读取中…</p>
                )}
                {keys.state === 'error' && (
                  <p className="agent-field-error" role="alert">
                    {keys.message}
                  </p>
                )}
                {keys.state === 'ready' && compatibleKeys.length === 0 && (
                  <p className="agent-field-error" role="alert">
                    没有兼容的 Key。
                  </p>
                )}
                {keyError && (
                  <p className="agent-field-error" id="agent-key-error" role="alert">
                    {keyError}
                  </p>
                )}
              </>
            )}

            {credentialType === 'auth' && (
              <>
                {!authFiles?.length && <p className="agent-field-hint">
                  {providerLoginFileNames(preferredHarness).join('、')}
                  {providerLoginFileLocation(preferredHarness) && <InfoTip
                    label="登录文件位置"
                    icon="warning-circle"
                    content={providerLoginFileLocation(preferredHarness)}
                  />}
                </p>}
                <Button
                  ref={authRef}
                  type="button"
                  variant="secondary"
                  disabled={pending || pickingAuth}
                  onClick={() => void chooseAuthFiles()}
                >
                  {pickingAuth ? '读取中…' : preferredHarness === 'pi' ? '选择两个登录文件' : '选择登录文件'}
                </Button>
                {authFiles && authFiles.length > 0 && (
                  <p className="agent-field-hint" role="status">
                    已选择：{authFiles.map((file) => file.filename).join('、')}
                  </p>
                )}
                {authError && (
                  <p className="agent-field-error" role="alert">
                    {authError}
                  </p>
                )}
              </>
            )}
          </>
        )}

        <div className="agent-form-actions">
          <Button
            type="submit"
            variant="primary"
            loading={pending}
            disabled={submitDisabled || pickingAuth}
            title={submitDisabled ? submitDisabledReason : undefined}
          >
            {pending
              ? '处理中…'
              : mode === 'create'
                ? '创建 Agent'
                : previewMode
                  ? '保存修改'
                  : '保存名称'}
          </Button>
          <Button
            variant="secondary"
            onClick={onCancel}
          >
            取消
          </Button>
        </div>

        {message && (
          <p
            className="agent-form-message"
            role={message.tone === 'error' ? 'alert' : 'status'}
          >
            {message.text}
          </p>
        )}
      </form>
    </Card>
  );
}

export function agentDraftOf(agent: ManagedAgentView): AgentDraft {
  return {
    displayName: agent.displayName,
    role: agent.role,
    modelServiceId: agent.modelServiceId ?? '',
    organizationId: agent.organizationId,
  };
}
export function AgentDetailAvatar({ agent }: { readonly agent: ManagedAgentView }) {
  return (
    <AgentAvatar name={agent.displayName} tone={agent.avatarTone} size="lg" />
  );
}
