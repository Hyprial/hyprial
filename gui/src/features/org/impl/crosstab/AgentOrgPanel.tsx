import { useEffect, useRef, useState, type Dispatch } from 'react';
import { usePageAvailable } from '../../../../app/page-availability';
import { Button, Field, Notice } from '../../../../components/primitives';
import type { AppAction } from '../../../../state/types';
import { MIGRATION_STATUS_LABELS, ORG_COPY, zh } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import { findOrg, orgDisplayName, sortOrgs } from '../domain/selectors';
import type { MigrationTicket, OrgResult } from '../domain/types';
import { OrgDialog } from '../dialogs/OrgDialog';
import { MigrationPlanView, MigrationProgressView } from '../dialogs/MigrationPlanView';
import { resultMessage } from '../workspace/api';
import { useBusy } from '../workspace/controls';
import { agentOrgs } from './agent-org';
import { MigrationWizardDialog } from './MigrationWizardDialog';
import '../styles/org-header.css';
import '../styles/org-content.css';
import '../styles/org-dialogs.css';

type Modal =
  | { readonly kind: 'join'; readonly orgId?: string }
  | { readonly kind: 'leave'; readonly orgId: string }
  | { readonly kind: 'migrate' }
  | { readonly kind: 'cancel-migration'; readonly ticket: MigrationTicket }
  | { readonly kind: 'rollback'; readonly ticket: MigrationTicket };

const TERMINAL: readonly MigrationTicket['status'][] = ['completed', 'failed', 'cancelled', 'rejected'];

/**
 * Agent 详情中的组织相关分区（第 7 节）：所属组织、通信、迁移（源端）。
 * 只在有组织数据源时渲染；数据源没有实现的动作不渲染控件，没有任何控件也没有数据的分区不渲染。
 */
export function AgentOrgPanel({
  agentId,
  agentName,
  dispatch,
}: {
  readonly agentId: string;
  readonly agentName: string;
  readonly dispatch: Dispatch<AppAction>;
}) {
  const domain = useOrgDomain();
  const { snapshot, gateway, agentIntent, clearAgentIntent } = domain;
  const [modal, setModal] = useState<Modal | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const orgsRef = useRef<HTMLHeadingElement>(null);
  const migrationRef = useRef<HTMLHeadingElement>(null);
  const orgPageAvailable = usePageAvailable('org');

  useEffect(() => {
    if (!agentIntent || (agentIntent.agentId && agentIntent.agentId !== agentId)) {
      return;
    }
    if (agentIntent.joinOrgId) {
      if (gateway.joinAgentOrg) setModal({ kind: 'join', orgId: agentIntent.joinOrgId });
    }
    const target =
      agentIntent.section === 'migration'
        ? migrationRef
        : agentIntent.section === 'orgs'
          ? orgsRef
          : null;
    target?.current?.focus();
    clearAgentIntent();
  }, [agentIntent, agentId, clearAgentIntent]);

  if (!domain.provided || snapshot.status === 'unwired') {
    return null;
  }

  const { joinAgentOrg, leaveAgentOrg, checkMigration, planMigration, submitMigration } = gateway;
  const { startMigration, cancelMigration, rollbackMigration } = gateway;
  const canMigrate = checkMigration && planMigration && submitMigration;
  const ticketControls = startMigration || cancelMigration || rollbackMigration;
  const readState =
    snapshot.status === 'loading'
      ? ORG_COPY.loadingOrgs
      : snapshot.status === 'error'
        ? (snapshot.readError ?? ORG_COPY.unknown)
        : null;
  const orgs = agentOrgs(snapshot, agentId);
  const ticket = snapshot.outboundMigrations[agentId] ?? null;

  function orgNameOf(orgId: string): string {
    const org = findOrg(snapshot, orgId);
    return org ? orgDisplayName(org) : ORG_COPY.orgUnknown;
  }

  async function act(task: () => Promise<OrgResult<unknown>>, success: string): Promise<void> {
    const result = await task();
    if (result.kind === 'unavailable') return;
    setMessage(result.kind === 'ok' ? success : resultMessage(result));
  }

  return (
    <div className="org-agent-panel" data-dev-ui="DEV-UI-ORG-015">
      <p className="org-agent-panel__live" role="status" aria-live="polite">
        {message}
      </p>

      {orgs !== null || joinAgentOrg || leaveAgentOrg ? (
      <section className="org-agent-block" aria-labelledby={`org-agent-orgs-${agentId}`}>
        <h3 id={`org-agent-orgs-${agentId}`} ref={orgsRef} tabIndex={-1}>
          所属组织
        </h3>
        {orgs === null ? (
          <p className="org-muted">{readState ?? ORG_COPY.orgUnknown}</p>
        ) : orgs.length === 0 ? (
          <p className="org-empty-inline">未关联组织。</p>
        ) : (
          <ul className="org-rows" aria-label="所属组织列表">
            {orgs.map((org) => (
              <li key={org.id} className="org-row">
                <span className="org-row__title">{orgDisplayName(org)}</span>
                {leaveAgentOrg ? (
                  <Button
                    size="sm"
                    onClick={() => setModal({ kind: 'leave', orgId: org.id })}
                  >
                    移出
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {joinAgentOrg ? (
          <div className="org-actions">
            <Button size="sm" onClick={() => setModal({ kind: 'join' })}>
              加入组织
            </Button>
          </div>
        ) : null}
      </section>

      ) : null}

      {orgPageAvailable && domain.tabShown('stats') ? (
        <section className="org-agent-block" aria-labelledby={`org-agent-comm-${agentId}`}>
          <h3 id={`org-agent-comm-${agentId}`}>通信</h3>
          <div className="org-actions">
            <Button
              size="sm"
              onClick={() => {
                domain.requestOrgView({ tab: 'stats', segment: agentId });
                dispatch({ type: 'navigate', page: 'org' });
              }}
            >
              查看统计
            </Button>
          </div>
        </section>
      ) : null}

      {canMigrate || (ticket && ticketControls) ? (
      <section className="org-agent-block" aria-labelledby={`org-agent-migration-${agentId}`}>
        <h3 id={`org-agent-migration-${agentId}`} ref={migrationRef} tabIndex={-1}>
          迁移
        </h3>
        {ticket && !TERMINAL.includes(ticket.status) ? (
          <MigrationStatus
            ticket={ticket}
            onRefresh={() => void act(() => gateway.refresh(null), '已刷新迁移单状态。')}
            onStart={
              startMigration
                ? () => void act(() => startMigration(ticket.id), '已开始迁移，进度以目标机器上报为准。')
                : undefined
            }
            onCancel={cancelMigration ? () => setModal({ kind: 'cancel-migration', ticket }) : undefined}
            onRollback={rollbackMigration ? () => setModal({ kind: 'rollback', ticket }) : undefined}
          />
        ) : (
          <>
            {ticket ? (
              <p className="org-muted">
                上一张迁移单 {ticket.ticketNo}：{MIGRATION_STATUS_LABELS[ticket.status]}
              </p>
            ) : null}
            {canMigrate ? (
              <Button size="sm" onClick={() => setModal({ kind: 'migrate' })}>
                迁移到组织内的机器
              </Button>
            ) : null}
          </>
        )}
      </section>

      ) : null}

      {modal?.kind === 'join' ? (
        <JoinAgentOrgDialog
          agentId={agentId}
          agentName={agentName}
          initialOrgId={modal.orgId}
          onClose={() => setModal(null)}
          onDone={setMessage}
        />
      ) : null}
      {modal?.kind === 'leave' ? (
        <ConfirmAction
          title={zh`把 ${agentName} 移出 ${orgNameOf(modal.orgId)}？`}
          lead="移出后，该组织的成员不能再通过这个组织与本 Agent 通信。"
          confirmLabel="移出"
          onClose={() => setModal(null)}
          onConfirm={() => leaveAgentOrg!(agentId, modal.orgId)}
          onDone={(result) => setMessage(
            result.kind === 'ok'
              ? '已移出。'
              : resultMessage(result),
          )}
        />
      ) : null}
      {modal?.kind === 'cancel-migration' ? (
        <ConfirmAction
          title={`取消迁移单 ${modal.ticket.ticketNo}？`}
          lead={
            modal.ticket.status === 'running'
              ? '迁移已开始。取消后迁移单结束，已传输部分以目标机器上报为准；再迁移需重新发起。'
              : '取消后迁移单结束。要再迁移，需要重新发起，双方重新同意。'
          }
          confirmLabel="确认取消迁移"
          dismissLabel="返回"
          onClose={() => setModal(null)}
          onConfirm={() => cancelMigration!(modal.ticket.id)}
          onDone={(result) =>
            setMessage(result.kind === 'ok' ? `迁移单 ${modal.ticket.ticketNo} 已取消。` : resultMessage(result))
          }
        />
      ) : null}
      {modal?.kind === 'rollback' ? (
        <ConfirmAction
          title={`回滚迁移单 ${modal.ticket.ticketNo}？`}
          lead="回滚按迁移单里的回滚条件执行，结果以目标机器上报为准。"
          confirmLabel="确认回滚"
          dismissLabel="返回"
          onClose={() => setModal(null)}
          onConfirm={() => rollbackMigration!(modal.ticket.id)}
          onDone={(result) => setMessage(result.kind === 'ok' ? '已提交回滚。' : resultMessage(result))}
        />
      ) : null}
      {modal?.kind === 'migrate' ? (
        <MigrationWizardDialog
          agentId={agentId}
          agentName={agentName}
          onClose={() => setModal(null)}
          onSubmitted={(next) =>
            setMessage(`迁移单 ${next.ticketNo} 已发起：${MIGRATION_STATUS_LABELS[next.status]}`)
          }
        />
      ) : null}
    </div>
  );
}

function MigrationStatus({
  ticket,
  onRefresh,
  onStart,
  onCancel,
  onRollback,
}: {
  readonly ticket: MigrationTicket;
  readonly onRefresh: () => void;
  readonly onStart?: () => void;
  readonly onCancel?: () => void;
  readonly onRollback?: () => void;
}) {
  return (
    <div className="org-migration-status">
      <p className="org-row__title">
        迁移单 {ticket.ticketNo} · {MIGRATION_STATUS_LABELS[ticket.status]}
      </p>
      {ticket.status === 'awaiting-target' ? (
        <p className="org-muted">
          请目标方在目标机器上处理这张迁移单（编号 {ticket.ticketNo}）。目标方处理后点“刷新”。
        </p>
      ) : null}
      {ticket.status === 'plan-conflict' ? (
        <Notice tone="warning">计划冲突：只能取消后重新发起，双方重新同意。</Notice>
      ) : null}
      {/* 迁移开始后进度最重要，放在计划前面。 */}
      {ticket.progress ? <MigrationProgressView progress={ticket.progress} /> : null}
      <MigrationPlanView ticket={ticket} />
      <div className="org-actions">
        <Button size="sm" onClick={onRefresh}>
          刷新
        </Button>
        {onStart && (ticket.status === 'awaiting-target' || ticket.status === 'verifying' || ticket.status === 'ready') ? (
          <Button
            size="sm"
            variant="primary"
            disabled={ticket.status !== 'ready'}
            title={ticket.status !== 'ready' ? '迁移单状态为“可以开始”后才能开始' : undefined}
            onClick={onStart}
          >
            开始迁移
          </Button>
        ) : null}
        {onCancel ? (
          <Button size="sm" variant="danger" onClick={onCancel}>
            取消迁移
          </Button>
        ) : null}
        {onRollback && ticket.rollbackAllowed ? (
          <Button size="sm" onClick={onRollback}>
            回滚
          </Button>
        ) : null}
      </div>
      <p className="org-muted">进度以目标机器上报为准。</p>
    </div>
  );
}

function JoinAgentOrgDialog({
  agentId,
  agentName,
  initialOrgId,
  onClose,
  onDone,
}: {
  readonly agentId: string;
  readonly agentName: string;
  readonly initialOrgId?: string;
  readonly onClose: () => void;
  readonly onDone: (message: string | null) => void;
}) {
  const { snapshot, gateway } = useOrgDomain();
  const joinAgentOrg = gateway.joinAgentOrg;
  // 只列 Agent 还没加入的组织。
  const joined = snapshot.agentOrgs?.[agentId] ?? [];
  const options = snapshot.orgs
    ? sortOrgs(snapshot.orgs).filter((item) => !joined.includes(item.id))
    : [];
  const [orgId, setOrgId] = useState(
    options.find((item) => item.id === initialOrgId)?.id ?? options[0]?.id ?? '',
  );
  const [message, setMessage] = useState<string | null>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const { busy, run } = useBusy();
  const org = findOrg(snapshot, orgId);

  async function submit(): Promise<void> {
    if (!orgId || !joinAgentOrg) return;
    const result = await run(() => joinAgentOrg(agentId, orgId));
    if (!result || result.kind === 'unavailable') return;
    if (result.kind === 'ok') {
      onDone(zh`已加入 ${org ? orgDisplayName(org) : orgId}。`);
      onClose();
      return;
    }
    if (result.kind === 'submitted') {
      onDone(ORG_COPY.approvalPending);
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title={zh`把 ${agentName} 加入组织`}
      description="加入工作组织需要所有者审批。"
      onClose={onClose}
      initialFocusRef={selectRef}
      devUi="DEV-UI-ORG-015"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '加入组织',
          variant: 'primary',
          loading: busy,
          disabled: !orgId,
          onClick: () => void submit(),
        },
      ]}
    >
      {options.length === 0 ? (
        <p className="org-muted">
          {snapshot.orgs ? `${agentName} 已加入全部组织。` : ORG_COPY.orgUnknown}
        </p>
      ) : (
        <Field label="组织" htmlFor="org-agent-join-select">
          <select
            id="org-agent-join-select"
            ref={selectRef}
            className="org-input"
            value={orgId}
            onChange={(event) => setOrgId(event.target.value)}
          >
            {options.map((item) => (
              <option key={item.id} value={item.id}>
                {orgDisplayName(item)}
              </option>
            ))}
          </select>
        </Field>
      )}
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}

function ConfirmAction({
  title,
  lead,
  confirmLabel,
  dismissLabel = '取消',
  onClose,
  onConfirm,
  onDone,
}: {
  readonly title: string;
  readonly lead: string;
  readonly confirmLabel: string;
  /** 动作本身叫“取消…”时改用“返回”，避免两个“取消”混淆。 */
  readonly dismissLabel?: string;
  readonly onClose: () => void;
  readonly onConfirm: () => Promise<OrgResult>;
  readonly onDone: (result: OrgResult) => void;
}) {
  const leadRef = useRef<HTMLParagraphElement>(null);
  const [message, setMessage] = useState<string | null>(null);
  const { busy, run } = useBusy();
  return (
    <OrgDialog
      title={title}
      onClose={onClose}
      initialFocusRef={leadRef}
      devUi="DEV-UI-ORG-015"
      actions={[
        { label: dismissLabel, onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : confirmLabel,
          variant: 'danger',
          loading: busy,
          onClick: () =>
            void run(onConfirm).then((result) => {
              if (!result || result.kind === 'unavailable') return;
              if (result.kind === 'ok' || result.kind === 'submitted') {
                onDone(result);
                onClose();
              } else {
                setMessage(resultMessage(result));
              }
            }),
        },
      ]}
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        {lead}
      </p>
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}
