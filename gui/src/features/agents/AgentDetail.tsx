import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import type {
  AgentOrganization,
  ManagedAgentView,
} from '../../state/agent-selectors';
import type { AgentDraft, AgentMutationResult } from './AgentMutationGateway';
import type { AgentExtensionView } from './agent-extensions';
import { AgentForm, agentDraftOf } from './AgentForm';
import { AgentManagePanel } from './AgentManagePanel';
import { AgentAvatar } from '../../components/AgentAvatar';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/primitives';
import { AgentOrgSummary } from '../org/crosstab/AgentOrgBadges';

export interface AgentDetailProps {
  readonly agent: ManagedAgentView;
  readonly extension: AgentExtensionView;
  readonly organizations: readonly AgentOrganization[];
  readonly onNavigateConnections: () => void;
  readonly onSave: (draft: AgentDraft) => Promise<AgentMutationResult>;
  readonly onDelete: () => Promise<AgentMutationResult>;
  /**
   * Open the chat page with this agent as the active target. Undefined when
   * the agent has no chat-capable target (unbound credentials or an unknown
   * chat target) — the button then renders disabled instead of pretending.
   */
  readonly onStartChat?: () => void;
  /** dev-04：详情中的组织分区（所属组织、通信、迁移），只在查看模式显示。 */
  readonly orgSections?: ReactNode;
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
}: AgentDetailProps) {
  const [mode, setMode] = useState<Mode>('view');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<
    { tone: 'error' | 'status'; text: string } | null
  >(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    titleRef.current?.focus();
  }, [agent.id]);

  useEffect(() => {
    if (confirmOpen) {
      cancelRef.current?.focus();
    }
  }, [confirmOpen]);

  function closeConfirm(): void {
    setConfirmOpen(false);
    deleteButtonRef.current?.focus();
  }

  async function save(draft: AgentDraft): Promise<void> {
    setPending(true);
    setMessage(null);
    const result = await onSave(draft);
    setPending(false);
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

  async function confirmDelete(): Promise<void> {
    setPending(true);
    setMessage(null);
    const result = await onDelete();
    setPending(false);
    setConfirmOpen(false);
    if (result.kind === 'deleted') {
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

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      closeConfirm();
    }
  }

  return (
    // 原生 section：测试与共享选择器以 [data-selected="true"] 定位详情卡，
    // primitives Card 会覆写 data-selected，故此处不迁 Card 基件。
    <section
      className="card agent-panel agent-detail"
      data-selected="true"
      data-agent-id={agent.id}
      aria-labelledby="agent-detail-title"
    >
      {mode === 'edit' ? (
        <AgentForm
          mode="edit"
          initial={{
            ...agentDraftOf(agent),
            preferredHarness: extension.preferredHarness ?? null,
          }}
          organizations={organizations}
          pending={pending}
          message={message}
          onSubmit={(draft) => void save(draft)}
          onCancel={() => {
            setMode('view');
            setMessage(null);
          }}
        />
      ) : (
        <>
          <header className="agent-detail-head">
            <AgentAvatar
              name={agent.displayName}
              tone={agent.avatarTone}
              size="lg"
            />
            <div className="agent-detail-title">
              <h2 id="agent-detail-title" ref={titleRef} tabIndex={-1}>
                {agent.displayName}
                {extension.preset && (
                  <span className="badge badge-preset">预配置</span>
                )}
              </h2>
              <p>{agent.role}</p>
            </div>
          </header>

          <dl className="agent-detail-grid">
            <div>
              <dt>所有者</dt>
              <dd data-owner={agent.ownerLabel}>{agent.ownerLabel}</dd>
            </div>
            <div>
              <dt>主机</dt>
              <dd data-host={agent.host}>{agent.host}</dd>
            </div>
            <div>
              <dt>消息渠道</dt>
              <dd data-connection="channel">
                {agent.channel.label}
                <span className="agent-detail-source">{agent.channel.sourceLabel}</span>
              </dd>
            </div>
            <div>
              <dt>所属组织</dt>
              <dd>
                <AgentOrgSummary agentId={agent.id} fallback={agent.organizationLabel} />
              </dd>
            </div>
            <div>
              <dt>首选 harness</dt>
              <dd>{extension.preferredHarness ?? '未指定'}</dd>
            </div>
          </dl>

          <AgentManagePanel
            agent={agent}
            extension={extension}
            onNavigateConnections={onNavigateConnections}
          />

          <div className="agent-detail-badges">
            <Badge tone="neutral">{agent.channel.label}</Badge>
          </div>

          <div className="agent-detail-actions">
            <Button
              variant="primary"
              className="button button-strong"
              disabled={!onStartChat}
              onClick={() => onStartChat?.()}
            >
              开始对话
            </Button>
            <Button
              variant="secondary"
              className="button button-secondary"
              onClick={() => setMode('edit')}
            >
              编辑
            </Button>
            <button
              ref={deleteButtonRef}
              type="button"
              className="button button-secondary"
              onClick={() => setConfirmOpen(true)}
            >
              删除
            </button>
          </div>

          {message && (
            <p
              className="agent-form-message"
              role={message.tone === 'error' ? 'alert' : 'status'}
            >
              {message.text}
            </p>
          )}
          {orgSections}
        </>
      )}

      {confirmOpen && (
        <div className="agent-dialog-layer">
          <div
            className="agent-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="agent-delete-title"
            aria-describedby="agent-delete-desc"
            onKeyDown={handleDialogKeyDown}
          >
            <h2 id="agent-delete-title">删除 Agent？</h2>
            <p id="agent-delete-desc">
              删除后不会出现在列表中，此操作不可撤销。
            </p>
            <div className="agent-dialog-actions">
              <button
                ref={cancelRef}
                type="button"
                className="button button-secondary"
                onClick={closeConfirm}
              >
                取消
              </button>
              <Button
                variant="primary"
                className="button button-strong"
                loading={pending}
                onClick={() => void confirmDelete()}
              >
                {pending ? '处理中…' : '确认删除'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
