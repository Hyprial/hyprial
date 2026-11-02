import { useState } from 'react';
import { Button } from '../../../components/primitives';
import { OrgMachines } from '../../../features/network/OrgMachines';
import {
  buildUnavailableMachineSectionModel,
  type MachineSectionModel,
} from '../../../features/network/machine-view-model';
import { MACHINE_STATUS_LABELS, MIGRATION_STATUS_LABELS, ORG_COPY } from '../domain/copy';
import type { MigrationTicket, OrgMachineRecord } from '../domain/types';
import { MigrationTicketDialog } from '../dialogs/MigrationTicketDialog';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import { Keep, MetaParts, SectionHeader, SidePanel, UnknownBlock } from '../workspace/controls';

/**
 * 机器（5.5）：沿用 J6 的字段，增加状态未知、同步中、休眠与“立即重试”，
 * 以及“这台电脑”侧栏中的迁入请求（迁移目标方）。不提供移除机器。
 * 未接通时沿用 J6 的“机器网络暂不可用”。
 */
export function MachinesSection({
  api,
  fallback,
}: {
  readonly api: WorkspaceApi;
  readonly fallback?: MachineSectionModel;
}) {
  const { snapshot, detail, org } = api;
  const [panelOpen, setPanelOpen] = useState(api.intent?.panel === 'this-computer');
  const [ticket, setTicket] = useState<MigrationTicket | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [retried, setRetried] = useState<ReadonlySet<string>>(() => new Set());

  if (snapshot.status === 'unwired' || (fallback && org && detail?.machines == null)) {
    return (
      <div className="org-section" data-org-section="machines">
        <OrgMachines model={fallback ?? buildUnavailableMachineSectionModel()} />
      </div>
    );
  }

  const machines = detail?.machines ?? null;
  const inbound = snapshot.inboundMigrations;

  async function retry(machine: OrgMachineRecord): Promise<void> {
    if (!org) return;
    setRetrying(machine.id);
    const result = await api.gateway.retryMachine(org.id, machine.id);
    setRetrying(null);
    if (result.kind === 'ok' || result.kind === 'submitted') {
      // 结果以后端为准：机器状态变化前，这一行标出“已提交重试”，不重复提交。
      setRetried((prev) => new Set(prev).add(machine.id));
      api.notify(`已对 ${machine.hostname} 发起重试，等待结果。`);
      return;
    }
    api.notify(resultMessage(result) ?? '');
  }

  return (
    <div className="org-section" data-org-section="machines">
      <SectionHeader
        title="机器"
        description="只列出已加入组织的设备；“同步中”表示该设备的信息还没同步到本机，不需要审批。"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <div className={`org-master-detail${panelOpen ? ' has-panel' : ''}`}>
        <div className="org-master">
          {machines === null ? (
            <UnknownBlock onRefresh={() => void api.refresh()} />
          ) : machines.length === 0 ? (
            <p className="org-empty-inline">{ORG_COPY.noMachines}</p>
          ) : (
            <ul className="org-rows" aria-label="组织机器列表">
              {machines.map((machine) => (
                <li key={machine.id} className="org-row" data-org-machine={machine.id}>
                  <div
                    className="org-row__main org-row__main--static"
                    aria-current={panelOpen && machine.current ? 'true' : undefined}
                  >
                    <span className="org-row__title">
                      {machine.hostname}
                      {machine.current ? (
                        <span className="org-tag org-tag--current">{ORG_COPY.thisComputer}</span>
                      ) : null}
                    </span>
                    <span className="org-row__meta">
                      <MetaParts
                        parts={[machine.system, `加入于 ${machine.joinedAt}`, `主人 ${machine.ownerLabel}`]}
                      />
                    </span>
                    {machine.status === 'dormant' && machine.dormant ? (
                      <span className="org-row__meta">
                        重试预算已用完：失败 {machine.dormant.failures} 次，最后尝试{' '}
                        <Keep>{machine.dormant.lastAttemptAt}</Keep>
                      </span>
                    ) : null}
                    {machine.status === 'dormant' && retried.has(machine.id) ? (
                      <span className="org-row__meta">已提交重试，等待结果</span>
                    ) : null}
                  </div>
                  <span className={`org-status org-status--${machine.status}`}>
                    {MACHINE_STATUS_LABELS[machine.status]}
                  </span>
                  {machine.status === 'dormant' ? (
                    <Button
                      size="sm"
                      loading={retrying === machine.id}
                      disabled={retried.has(machine.id)}
                      onClick={() => void retry(machine)}
                    >
                      {ORG_COPY.retryNow}
                    </Button>
                  ) : null}
                  {machine.current ? (
                    <Button size="sm" onClick={() => setPanelOpen(true)}>
                      查看{ORG_COPY.thisComputer}
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>

        {panelOpen ? (
          <SidePanel parentLabel="机器" title={ORG_COPY.thisComputer} onClose={() => setPanelOpen(false)}>
            <dl className="org-facts">
              <div>
                <dt>主机名</dt>
                <dd>{snapshot.thisComputer?.hostname ?? ORG_COPY.unknown}</dd>
              </div>
              <div>
                <dt>系统</dt>
                <dd>{snapshot.thisComputer?.system ?? ORG_COPY.unknown}</dd>
              </div>
            </dl>
            <h4 className="org-subheading">{ORG_COPY.inboundRequests}</h4>
            {inbound === null ? (
              <p className="org-unavailable" role="note">
                {ORG_COPY.migrationUnavailable}
              </p>
            ) : inbound.length === 0 ? (
              <p className="org-empty-inline">{ORG_COPY.noInbound}</p>
            ) : (
              <ul className="org-rows" aria-label={ORG_COPY.inboundRequests}>
                {inbound.map((item) => (
                  <li key={item.id} className="org-row" data-org-inbound={item.id}>
                    <button
                      type="button"
                      className="org-row__main"
                      aria-label={`打开迁移单 ${item.ticketNo}`}
                      onClick={() => setTicket(item)}
                    >
                      <span className="org-row__title">
                        {item.ticketNo} · {item.agentName}
                      </span>
                      <span className="org-row__meta">
                        <MetaParts parts={[`来自 ${item.sourceMachine}`, `发起于 ${item.startedAt}`]} />
                      </span>
                    </button>
                    <span className="org-status">{MIGRATION_STATUS_LABELS[item.status]}</span>
                  </li>
                ))}
              </ul>
            )}
          </SidePanel>
        ) : null}
      </div>

      {ticket ? (
        <MigrationTicketDialog
          api={api}
          ticket={inbound?.find((item) => item.id === ticket.id) ?? ticket}
          onClose={() => setTicket(null)}
        />
      ) : null}
    </div>
  );
}
