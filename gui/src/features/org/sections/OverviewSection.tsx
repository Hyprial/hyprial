import { useState } from 'react';
import { Button, Notice } from '../../../components/primitives';
import { MACHINE_STATUS_LABELS, ORG_COPY } from '../domain/copy';
import { hasWorkOrg } from '../domain/selectors';
import type { MachineStatus, OrgDetail, OrgTab } from '../domain/types';
import { RepairDialog } from '../dialogs/RepairDialog';
import type { WorkspaceApi } from '../workspace/api';
import { SectionHeader, UnknownBlock } from '../workspace/controls';

interface CountCard {
  readonly tab: OrgTab;
  readonly segment?: string;
  readonly label: string;
  readonly value: string;
  /** 计数下方的一行说明：同步状态、归属或机器状态分布。 */
  readonly note?: string;
}

const MACHINE_ORDER: readonly MachineStatus[] = ['online', 'syncing', 'unknown', 'offline', 'dormant'];

function joinNotes(parts: readonly (string | false)[]): string | undefined {
  const kept = parts.filter((part): part is string => Boolean(part));
  return kept.length > 0 ? kept.join(' · ') : undefined;
}

/** 计数卡：null 为“状态未确认”，不当作 0。 */
function countCards(detail: OrgDetail | null): readonly CountCard[] {
  const people = detail?.people ?? null;
  const agents = detail?.agents ?? null;
  const machines = detail?.machines ?? null;
  const count = (items: readonly unknown[] | null) => (items === null ? ORG_COPY.unknown : String(items.length));
  const syncing = people?.filter((item) => item.sync === 'syncing').length ?? 0;
  const unknown = people?.filter((item) => item.sync === 'unknown').length ?? 0;
  const mine = agents?.filter((item) => item.mine).length ?? 0;
  return [
    {
      tab: 'members',
      label: '成员',
      value: count(people),
      note: joinNotes([syncing > 0 && `${syncing} 人同步中`, unknown > 0 && `${unknown} 人状态未确认`]),
    },
    {
      tab: 'members',
      segment: 'agents',
      label: 'Agent',
      value: count(agents),
      note: agents
        ? joinNotes([`我的 ${mine}`, agents.length > mine && `他人的 ${agents.length - mine}`])
        : undefined,
    },
    {
      tab: 'machines',
      label: '机器',
      value: count(machines),
      note: machines
        ? joinNotes(
            MACHINE_ORDER.map((status) => {
              const n = machines.filter((item) => item.status === status).length;
              return n > 0 && `${MACHINE_STATUS_LABELS[status]} ${n}`;
            }),
          )
        : undefined,
    },
  ];
}

/**
 * 概览（5.2）：三种情况——没有可读的组织、只有个人组织、工作组织。
 * 计数卡可以跳到对应子页签；所有者在工作组织中另见“邀请成员”。
 */
export function OverviewSection({ api }: { readonly api: WorkspaceApi }) {
  const { snapshot, org, detail, perms } = api;
  const [repairOpen, setRepairOpen] = useState(false);

  if (snapshot.orgs === null) {
    return (
      <div className="org-section" data-org-section="overview">
        <UnknownBlock
          detail="组织数据暂不可用。需要时可以从组织切换器新建组织，或用邀请链接加入。"
          onRefresh={snapshot.status === 'ready' ? () => void api.refresh() : undefined}
        />
      </div>
    );
  }

  if (!org) {
    return (
      <div className="org-section" data-org-section="overview">
        <div className="org-empty" role="status">
          <p className="org-empty__title">{ORG_COPY.noOrgsTitle}</p>
          <div className="org-actions">
            <Button variant="primary" onClick={api.openCreateOrg}>
              {ORG_COPY.createOrg}
            </Button>
            <Button onClick={() => api.goTo('invites', { segment: 'join' })}>
              {ORG_COPY.joinByLink}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const cards = countCards(detail);
  const abilities =
    org.kind === 'personal'
      ? ORG_COPY.personalAbilities
      : org.role === 'owner'
        ? ORG_COPY.ownerAbilities
        : ORG_COPY.memberAbilities;
  const unfinished = org.lifecycle === 'creating' || org.lifecycle === 'partial';

  return (
    <div className="org-section" data-org-section="overview">
      <SectionHeader
        title="概览"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />

      {unfinished ? (
        <Notice
          tone="warning"
          title={ORG_COPY.creatingWarning}
          action={
            org.role === 'owner'
              ? { label: ORG_COPY.repair, onClick: () => setRepairOpen(true) }
              : undefined
          }
        >
          组织创建还没有完成。所有者可以先预览修复内容再确认执行，也可以用同一个名称重新创建。
        </Notice>
      ) : null}
      {org.lifecycle === 'sync-problem' ? (
        <Notice tone="warning" title="同步异常">
          组织数据同步异常，列表可能不完整；刷新后再核对。
        </Notice>
      ) : null}

      <div className="org-count-grid">
        {cards.map((card) => (
          <button
            key={card.label}
            type="button"
            className="org-count-card"
            aria-label={`${card.label}：${card.value}${card.note ? `，${card.note}` : ''}。查看${card.tab === 'machines' ? '机器' : card.label}`}
            onClick={() => api.goTo(card.tab, card.segment ? { segment: card.segment } : undefined)}
          >
            <span className="org-count-card__label">{card.label}</span>
            <span className="org-count-card__value">{card.value}</span>
            {card.note ? <span className="org-count-card__note">{card.note}</span> : null}
          </button>
        ))}
      </div>

      <p className="org-abilities">{abilities}</p>

      <div className="org-actions" aria-label="快捷动作">
        {perms.invite ? (
          <Button variant="primary" onClick={() => api.goTo('invites', { segment: 'send' })}>
            {ORG_COPY.inviteMember}
          </Button>
        ) : null}
        <Button
          onClick={() => {
            api.requestAgentView({ createInOrgId: org.id });
            api.dispatch({ type: 'open-agent-create' });
          }}
        >
          {ORG_COPY.newAgentHere}
        </Button>
        {!api.isCurrent ? (
          <Button onClick={() => void api.setCurrent(org.id)}>{ORG_COPY.setCurrent}</Button>
        ) : null}
      </div>

      {org.kind === 'personal' && !hasWorkOrg(snapshot) ? (
        <section className="org-guide" aria-labelledby="org-guide-title">
          <h3 id="org-guide-title">{ORG_COPY.guideTitle}</h3>
          <p>和同事一起使用时，新建一个工作组织，或用收到的邀请链接加入。</p>
          <div className="org-actions">
            <Button variant="primary" onClick={api.openCreateOrg}>
              {ORG_COPY.createOrg}
            </Button>
            <Button onClick={() => api.goTo('invites', { segment: 'join' })}>
              {ORG_COPY.joinByLink}
            </Button>
          </div>
        </section>
      ) : null}

      {repairOpen ? (
        <RepairDialog api={api} orgId={org.id} onClose={() => setRepairOpen(false)} />
      ) : null}
    </div>
  );
}
