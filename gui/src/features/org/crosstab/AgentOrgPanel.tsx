import { useEffect, useRef, useState, type Dispatch } from 'react';
import { Button, Field, Notice } from '../../../components/primitives';
import type { AppAction } from '../../../state/types';
import { MIGRATION_STATUS_LABELS, ORG_COPY, zh } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import { findOrg, orgDisplayName, sortOrgs } from '../domain/selectors';
import type { AgentLink, MigrationTicket, OrgResult } from '../domain/types';
import { OrgDialog } from '../dialogs/OrgDialog';
import { MigrationPlanView, MigrationProgressView } from '../dialogs/MigrationPlanView';
import { resultMessage } from '../workspace/api';
import { Keep, MetaParts, useBusy } from '../workspace/controls';
import { agentOrgs } from './agent-org';
import { MigrationWizardDialog } from './MigrationWizardDialog';
import '../workspace/workspace.css';

type Modal =
  | { readonly kind: 'join'; readonly orgId?: string }
  | { readonly kind: 'leave'; readonly orgId: string }
  | { readonly kind: 'add-link'; readonly peer?: string }
  | { readonly kind: 'revoke'; readonly link: AgentLink }
  | { readonly kind: 'migrate' }
  | { readonly kind: 'cancel-migration'; readonly ticket: MigrationTicket }
  | { readonly kind: 'rollback'; readonly ticket: MigrationTicket };

const TERMINAL: readonly MigrationTicket['status'][] = ['completed', 'failed', 'cancelled', 'rejected'];

/**
 * Agent 详情中的组织相关分区（第 7 节）：所属组织、通信、迁移（源端）。
 * 只在有组织数据源时渲染；未接通时各区显示“状态未确认”或“暂不可用”，不发请求。
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
  const commRef = useRef<HTMLHeadingElement>(null);
  const migrationRef = useRef<HTMLHeadingElement>(null);

  // 从聊天修复入口或组织页进入时，定位分区并预填对象。
  useEffect(() => {
    if (!agentIntent || (agentIntent.agentId && agentIntent.agentId !== agentId)) {
      return;
    }
    if (agentIntent.joinOrgId) {
      setModal({ kind: 'join', orgId: agentIntent.joinOrgId });
    } else if (agentIntent.prefillPeer) {
      setModal({ kind: 'add-link', peer: agentIntent.prefillPeer });
    }
    const target =
      agentIntent.section === 'comm'
        ? commRef
        : agentIntent.section === 'migration'
          ? migrationRef
          : agentIntent.section === 'orgs'
            ? orgsRef
            : null;
    target?.current?.focus();
    clearAgentIntent();
  }, [agentIntent, agentId, clearAgentIntent]);

  if (!domain.provided) {
    return null;
  }

  const unwired = snapshot.status === 'unwired';
  const orgs = agentOrgs(snapshot, agentId);
  const links = snapshot.agentLinks?.[agentId] ?? (snapshot.agentLinks ? [] : null);
  const refused = snapshot.refusedMessages?.[agentId] ?? (snapshot.refusedMessages ? [] : null);
  const ticket = snapshot.outboundMigrations[agentId] ?? null;

  function orgNameOf(orgId: string): string {
    const org = findOrg(snapshot, orgId);
    return org ? orgDisplayName(org) : ORG_COPY.orgUnknown;
  }

  async function act(task: () => Promise<OrgResult<unknown>>, success: string): Promise<void> {
    const result = await task();
    setMessage(result.kind === 'ok' ? success : resultMessage(result));
  }

  return (
    <div className="org-agent-panel" data-dev-ui="DEV-UI-ORG-015">
      <p className="org-agent-panel__live" role="status" aria-live="polite">
        {message}
      </p>

      <section className="org-agent-block" aria-labelledby={`org-agent-orgs-${agentId}`}>
        <h3 id={`org-agent-orgs-${agentId}`} ref={orgsRef} tabIndex={-1}>
          所属组织
        </h3>
        {orgs === null ? (
          <p className="org-muted">{ORG_COPY.orgUnknown}</p>
        ) : (
          <ul className="org-rows" aria-label="所属组织列表">
            {orgs.map((org) => (
              <li key={org.id} className="org-row">
                <span className="org-row__title">{orgDisplayName(org)}</span>
                <Button
                  size="sm"
                  disabled={orgs.length <= 1}
                  title={orgs.length <= 1 ? ORG_COPY.agentLastOrg : undefined}
                  onClick={() => setModal({ kind: 'leave', orgId: org.id })}
                >
                  移出
                </Button>
              </li>
            ))}
          </ul>
        )}
        {orgs !== null && orgs.length <= 1 ? (
          <p className="org-muted">{ORG_COPY.agentLastOrg}</p>
        ) : null}
        <div className="org-actions">
          <Button size="sm" onClick={() => setModal({ kind: 'join' })}>
            加入组织
          </Button>
        </div>
      </section>

      <section className="org-agent-block" aria-labelledby={`org-agent-comm-${agentId}`}>
        <h3 id={`org-agent-comm-${agentId}`} ref={commRef} tabIndex={-1}>
          通信
        </h3>
        <h4 className="org-subheading">可以发送的对象</h4>
        <p className="org-muted">
          只列本 Agent 持有的 link：本 Agent → 对端。对端能否向本 Agent 发送，取决于对端是否持有指向本 Agent 的 link。
        </p>
        {links === null ? (
          <p className="org-muted">{ORG_COPY.unknown}</p>
        ) : links.length === 0 ? (
          <p className="org-empty-inline">还没有可以发送的对象。</p>
        ) : (
          <ul className="org-rows" aria-label="可以发送的对象">
            {links.map((link) => (
              <li key={link.id} className="org-row">
                <div className="org-row__main org-row__main--static">
                  <span className="org-row__title">
                    {agentName} <span aria-hidden="true">→</span>
                    <span className="ui-visually-hidden">可以发送给</span> {link.peerLabel}
                  </span>
                  <span className="org-row__meta">
                    <MetaParts
                      parts={[
                        link.state === 'active' ? '可用' : ORG_COPY.unknown,
                        `在途 ${link.inFlight}`,
                        `最近使用 ${link.lastUsedAt ?? '暂无'}`,
                      ]}
                    />
                  </span>
                </div>
                <Button size="sm" onClick={() => setModal({ kind: 'revoke', link })}>
                  收回
                </Button>
              </li>
            ))}
          </ul>
        )}
        <div className="org-actions">
          <Button size="sm" onClick={() => setModal({ kind: 'add-link' })}>
            添加可以发送的对象
          </Button>
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
        <h4 className="org-subheading">最近被拒的消息</h4>
        {refused === null ? (
          <p className="org-muted">{unwired ? '最近被拒的消息暂不可用' : ORG_COPY.unknown}</p>
        ) : refused.length === 0 ? (
          <p className="org-empty-inline">最近没有被拒的消息。</p>
        ) : (
          <ul className="org-rows" aria-label="最近被拒的消息">
            {refused.map((item) => (
              <li key={item.id} className="org-row">
                <div className="org-row__main org-row__main--static">
                  <span className="org-row__title">发给 {item.peerLabel}</span>
                  <span className="org-row__meta">
                    {item.reason} · <Keep>{item.at}</Keep>
                  </span>
                </div>
                <Button size="sm" onClick={() => setModal({ kind: 'add-link', peer: item.peerLabel })}>
                  添加可以发送的对象
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="org-agent-block" aria-labelledby={`org-agent-migration-${agentId}`}>
        <h3 id={`org-agent-migration-${agentId}`} ref={migrationRef} tabIndex={-1}>
          迁移
        </h3>
        {unwired ? (
          <>
            <Button size="sm" disabled>
              迁移
            </Button>
            <p className="org-muted">{ORG_COPY.migrationUnavailable}</p>
          </>
        ) : ticket && !TERMINAL.includes(ticket.status) ? (
          <MigrationStatus
            ticket={ticket}
            onRefresh={() => void act(() => gateway.refresh(null), '已刷新迁移单状态。')}
            onStart={() => void act(() => gateway.startMigration(ticket.id), '已开始迁移，进度以目标机器上报为准。')}
            onCancel={() => setModal({ kind: 'cancel-migration', ticket })}
            onRollback={() => setModal({ kind: 'rollback', ticket })}
          />
        ) : (
          <>
            {ticket ? (
              <p className="org-muted">
                上一张迁移单 {ticket.ticketNo}：{MIGRATION_STATUS_LABELS[ticket.status]}
              </p>
            ) : null}
            <Button size="sm" onClick={() => setModal({ kind: 'migrate' })}>
              迁移到组织内的机器
            </Button>
          </>
        )}
      </section>

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
          onConfirm={() => gateway.leaveAgentOrg(agentId, modal.orgId)}
          onDone={(result) => setMessage(result.kind === 'ok' ? '已移出。' : resultMessage(result))}
        />
      ) : null}
      {modal?.kind === 'add-link' ? (
        <AddLinkDialog
          agentName={agentName}
          initialPeer={modal.peer ?? ''}
          onClose={() => setModal(null)}
          onConfirm={(peer) => gateway.addLink(agentId, peer)}
          onDone={(result, peer) =>
            setMessage(result.kind === 'ok' ? zh`已添加：${agentName} 可以向 ${peer} 发送。` : resultMessage(result))
          }
        />
      ) : null}
      {modal?.kind === 'revoke' ? (
        <ConfirmAction
          title={zh`收回 ${agentName} → ${modal.link.peerLabel}？`}
          lead={zh`收回后，本 Agent 不能再向 ${modal.link.peerLabel} 发送；${modal.link.peerLabel} 能否向本 Agent 发送不受影响。`}
          confirmLabel="收回"
          onClose={() => setModal(null)}
          onConfirm={() => gateway.revokeLink(agentId, modal.link.id)}
          onDone={(result) => setMessage(result.kind === 'ok' ? '已收回。' : resultMessage(result))}
        />
      ) : null}
      {modal?.kind === 'cancel-migration' ? (
        <ConfirmAction
          title={`取消迁移单 ${modal.ticket.ticketNo}？`}
          lead={
            modal.ticket.status === 'running'
              ? '迁移已经开始。取消后迁移单结束；已传输部分的处理以目标机器上报为准。要再迁移，需要重新发起，双方重新同意。'
              : '取消后迁移单结束。要再迁移，需要重新发起，双方重新同意。'
          }
          confirmLabel="确认取消迁移"
          dismissLabel="返回"
          onClose={() => setModal(null)}
          onConfirm={() => gateway.cancelMigration(modal.ticket.id)}
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
          onConfirm={() => gateway.rollbackMigration(modal.ticket.id)}
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
  readonly onStart: () => void;
  readonly onCancel: () => void;
  readonly onRollback: () => void;
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
        {ticket.status === 'awaiting-target' || ticket.status === 'verifying' || ticket.status === 'ready' ? (
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
        <Button size="sm" variant="danger" onClick={onCancel}>
          取消迁移
        </Button>
        {ticket.rollbackAllowed ? (
          <Button size="sm" onClick={onRollback}>
            回滚
          </Button>
        ) : null}
      </div>
      <p className="org-muted">进度以目标机器上报为准，不估算剩余时间。</p>
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
  // 第 7 节：审批状态设计好之前，选择工作组织后显示“暂不可用”，不提交。
  const workPending = org?.kind === 'work';

  async function submit(): Promise<void> {
    if (!orgId || workPending) return;
    const result = await run(() => gateway.joinAgentOrg(agentId, orgId));
    if (!result) return;
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
      description="个人组织由主人确认即可；工作组织需要所有者审批。"
      onClose={onClose}
      initialFocusRef={selectRef}
      devUi="DEV-UI-ORG-015"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '提交',
          variant: 'primary',
          loading: busy,
          disabled: !orgId || workPending,
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
      {workPending ? (
        <p className="org-muted" role="note">
          {ORG_COPY.agentJoinWorkUnavailable}
        </p>
      ) : null}
      {message ? (
        <Notice tone="warning" role="alert">
          {message}
        </Notice>
      ) : null}
    </OrgDialog>
  );
}

function AddLinkDialog({
  agentName,
  initialPeer,
  onClose,
  onConfirm,
  onDone,
}: {
  readonly agentName: string;
  readonly initialPeer: string;
  readonly onClose: () => void;
  readonly onConfirm: (peer: string) => Promise<OrgResult>;
  readonly onDone: (result: OrgResult, peer: string) => void;
}) {
  const [peer, setPeer] = useState(initialPeer);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, run } = useBusy();

  async function submit(): Promise<void> {
    if (!peer.trim()) {
      setError('请填写对端');
      inputRef.current?.focus();
      return;
    }
    const result = await run(() => onConfirm(peer.trim()));
    if (!result) return;
    if (result.kind === 'ok') {
      onDone(result, peer.trim());
      onClose();
      return;
    }
    setMessage(resultMessage(result));
  }

  return (
    <OrgDialog
      title="添加可以发送的对象"
      description={peer.trim() ? zh`添加后，${agentName} 可以向 ${peer.trim()} 发送。` : zh`添加后，${agentName} 可以向对端发送。`}
      onClose={onClose}
      initialFocusRef={inputRef}
      devUi="DEV-UI-ORG-015"
      actions={[
        { label: '取消', onClick: onClose },
        {
          label: busy ? ORG_COPY.processing : '确认添加',
          variant: 'primary',
          loading: busy,
          onClick: () => void submit(),
        },
      ]}
    >
      <Field label="对端" htmlFor="org-link-peer" error={error ?? undefined} required>
        <input
          id="org-link-peer"
          ref={inputRef}
          className="org-input"
          autoComplete="off"
          value={peer}
          disabled={busy}
          onChange={(event) => setPeer(event.target.value)}
        />
      </Field>
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
              if (!result) return;
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
