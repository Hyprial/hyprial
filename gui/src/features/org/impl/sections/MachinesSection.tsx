import { useState } from 'react';
import { Button } from '../../../../components/primitives';
import { OrgMachines, type MachineSectionModel } from '../../../network';
import { MACHINE_STATUS_LABELS, MIGRATION_STATUS_LABELS, ORG_COPY } from '../domain/copy';
import type { MigrationTicket, OrgMachineRecord } from '../domain/types';
import { MigrationTicketDialog } from '../dialogs/MigrationTicketDialog';
import { resultMessage, type WorkspaceApi } from '../workspace/api';
import {
  Keep,
  MetaParts,
  OrgReadStateBlock,
  SectionHeader,
  SidePanel,
  UnknownBlock,
} from '../workspace/controls';

/**
 * 机器（5.5）：沿用 J6 的字段，增加状态未知、同步中、休眠与“立即重试”，
 * 以及“这台电脑”侧栏中的迁入请求（迁移目标方）。不提供移除机器。
 */
export function MachinesSection({
  api,
  fallback,
  heading = '机器',
  showRefresh = true,
}: {
  readonly api: WorkspaceApi;
  readonly fallback?: MachineSectionModel;
  readonly heading?: string;
  readonly showRefresh?: boolean;
}) {
  const { snapshot, detail, org } = api;
  const [panelOpen, setPanelOpen] = useState(api.intent?.panel === 'this-computer');
  const [ticket, setTicket] = useState<MigrationTicket | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [retried, setRetried] = useState<ReadonlySet<string>>(() => new Set());

  if (snapshot.status === 'loading' || snapshot.status === 'error') {
    return (
      <div className="org-section" data-org-section="machines">
        <SectionHeader title={heading} />
        <OrgReadStateBlock snapshot={snapshot} onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  if (fallback && org && detail?.machines == null) {
    return (
      <div className="org-section" data-org-section="machines">
        <OrgMachines model={fallback} />
      </div>
    );
  }

  const machines = detail?.machines ?? null;
  const inbound = snapshot.inboundMigrations;

  async function retry(machine: OrgMachineRecord): Promise<void> {
    if (!org || !api.guard(ORG_COPY.retryNow, 'retryMachine')) return;
    setRetrying(machine.id);
    const result = await api.gateway.retryMachine(org.id, machine.id);
    setRetrying(null);
    if (result.kind === 'ok' || result.kind === 'submitted') {
      // 结果以后端为准：机器状态变化前，这一行标出“已提交重试”，不重复提交。
      setRetried((prev) => new Set(prev).add(machine.id));
      api.notify(`已对 ${machine.hostname} 发起重试，等待结果。`);
      return;
    }
    if (result.kind === 'unavailable') return;
    api.notify(resultMessage(result) ?? '');
  }

  return (
    <div className="org-section" data-org-section="machines">
      <SectionHeader
        title={heading}
        description="“同步中”表示设备信息还没同步到本机，无需处理。"
        updatedAt={detail?.updatedAt}
        onRefresh={showRefresh ? () => void api.refresh() : undefined}
        refreshing={api.refreshing}
      />
      <div className={`org-master-detail${panelOpen ? ' has-panel' : ''}`}>
        <div className="org-master">
          {machines === null ? (
            <UnknownBlock onRefresh={showRefresh ? () => void api.refresh() : undefined} />
          ) : machines.length === 0 ? (
            <p className="org-empty-inline">{ORG_COPY.noMachines}</p>
          ) : (
            <ul className="org-rows" aria-label={`组织${heading}列表`}>
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
          <SidePanel parentLabel={heading} title={ORG_COPY.thisComputer} onClose={() => setPanelOpen(false)}>
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
            {inbound === null ? null : <h4 className="org-subheading">{ORG_COPY.inboundRequests}</h4>}
            {inbound === null ? null : inbound.length === 0 ? (
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
