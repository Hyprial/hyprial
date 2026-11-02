import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { AgentOrganization, ManagedAgentView } from '../../state/agent-selectors';
import type { AgentDraft } from './AgentMutationGateway';
import { AgentAvatar } from '../../components/AgentAvatar';
import { Button, Card, Select } from '../../components/primitives';
import { useAgentCreateOrgChoice } from '../org/crosstab/useAgentOrgChoice';

export interface AgentFormProps {
  readonly mode: 'create' | 'edit';
  readonly initial: AgentDraft;
  readonly organizations: readonly AgentOrganization[];
  readonly pending: boolean;
  readonly message: { readonly tone: 'error' | 'status'; readonly text: string } | null;
  readonly onSubmit: (draft: AgentDraft) => void;
  readonly onCancel: () => void;
}

const NAME_REQUIRED = '请填写 Agent 名称';
const ROLE_REQUIRED = '请填写 Agent 职责';

const HARNESS_OPTIONS = ['claude', 'codex', 'pi'] as const;

export function AgentForm({
  mode,
  initial,
  organizations,
  pending,
  message,
  onSubmit,
  onCancel,
}: AgentFormProps) {
  const [displayName, setDisplayName] = useState(initial.displayName);
  const [role, setRole] = useState(initial.role);
  // dev-04：组织数据就绪时，新建表单按组织设计第 7 节默认当前组织、不提供“不加入组织”。
  const orgChoice = useAgentCreateOrgChoice(mode === 'create');
  const [organizationId, setOrganizationId] = useState(
    orgChoice?.defaultId ?? initial.organizationId ?? '',
  );
  const [provider, setProvider] = useState(initial.provider ?? '');
  const [model, setModel] = useState(initial.model ?? '');
  const [preferredHarness, setPreferredHarness] = useState(
    initial.preferredHarness ?? '',
  );
  const [nameError, setNameError] = useState<string | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const roleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const title = mode === 'create' ? '创建 Agent' : '编辑 Agent';

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (pending) {
      return;
    }
    const name = displayName.trim();
    const roleText = role.trim();
    let firstInvalid: HTMLInputElement | null = null;
    if (!name) {
      setNameError(NAME_REQUIRED);
      firstInvalid = nameRef.current;
    } else {
      setNameError(null);
    }
    if (!roleText) {
      setRoleError(ROLE_REQUIRED);
      firstInvalid = firstInvalid ?? roleRef.current;
    } else {
      setRoleError(null);
    }
    if (firstInvalid) {
      firstInvalid.focus();
      return;
    }
    onSubmit({
      displayName: name,
      role: roleText,
      modelServiceId: '',
      organizationId: organizationId === '' ? null : organizationId,
      provider: provider.trim() === '' ? null : provider.trim(),
      model: model.trim() === '' ? null : model.trim(),
      preferredHarness: preferredHarness === '' ? null : preferredHarness,
    });
  }

  return (
    <Card
      as="section"
      className="card agent-panel"
      aria-labelledby="agent-form-title"
    >
      <div className="section-heading">
        <div>
          <h2 id="agent-form-title" tabIndex={-1}>
            {title}
          </h2>
          <p>
            名称与职责必填。模型凭据不在此处配置：创建后到详情页授予。
          </p>
        </div>
      </div>

      <form className="agent-form" onSubmit={handleSubmit} noValidate>
        <label htmlFor="agent-name">名称</label>
        <input
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

        <label htmlFor="agent-role">职责</label>
        <input
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

        <label htmlFor="agent-organization">所属组织</label>
        <Select
          id="agent-organization"
          value={organizationId}
          disabled={pending}
          onValueChange={setOrganizationId}
          options={
            orgChoice
              ? orgChoice.options.map((option) => ({ value: option.id, label: option.label }))
              : [
                  { value: '', label: '不加入组织' },
                  ...organizations.map((organization) => ({
                    value: organization.id,
                    label: organization.label,
                  })),
                ]
          }
        />
        {orgChoice ? <p className="agent-note">{orgChoice.note}</p> : null}

        <label htmlFor="agent-provider">服务商</label>
        <input
          id="agent-provider"
          autoComplete="off"
          value={provider}
          disabled={pending}
          placeholder="留空使用账号默认服务商"
          onChange={(event) => setProvider(event.target.value)}
        />
        <p className="agent-field-hint">可选，对应 --provider：模型厂商偏好。</p>

        <label htmlFor="agent-model">模型</label>
        <input
          id="agent-model"
          autoComplete="off"
          value={model}
          disabled={pending}
          placeholder="留空使用服务商默认模型"
          onChange={(event) => setModel(event.target.value)}
        />
        <p className="agent-field-hint">可选，对应 --model：模型 id 偏好。</p>

        <label htmlFor="agent-harness">首选 harness</label>
        <Select
          id="agent-harness"
          value={preferredHarness}
          disabled={pending}
          onValueChange={setPreferredHarness}
          options={[
            { value: '', label: '未指定' },
            ...HARNESS_OPTIONS.map((harness) => ({
              value: harness,
              label: harness,
            })),
          ]}
        />
        <p className="agent-field-hint">
          对应 --preferred-harness：仅作为默认，Agent 不被绑定到该 harness。
        </p>

        <div className="agent-form-actions">
          <Button
            type="submit"
            variant="primary"
            className="button button-strong"
            loading={pending}
          >
            {pending ? '处理中…' : mode === 'create' ? '创建 Agent' : '保存修改'}
          </Button>
          <Button
            variant="secondary"
            className="button button-secondary"
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
