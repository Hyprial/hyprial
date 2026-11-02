import { useEffect, useState, type Dispatch } from 'react';
import { Button, Notice, Select } from '../../../components/primitives';
import type { AppAction } from '../../../state/types';
import type { MachineSectionModel } from '../../../features/network/machine-view-model';
import type { TopologyModel } from '../../../features/network/topology-view-model';
import {
  ORG_COPY,
  ORG_KIND_LABELS,
  ORG_LIFECYCLE_LABELS,
  ORG_ROLE_LABELS,
  ORG_TAB_LABELS,
} from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import {
  findOrg,
  orgDetail,
  orgDisplayName,
  orgPermissions,
  pendingInvites,
  sortOrgs,
} from '../domain/selectors';
import type { OrgSummary, OrgTab } from '../domain/types';
import { CreateOrgDialog } from '../dialogs/CreateOrgDialog';
import { OverviewSection } from '../sections/OverviewSection';
import { MembersSection } from '../sections/MembersSection';
import { InvitesSection } from '../sections/InvitesSection';
import { MachinesSection } from '../sections/MachinesSection';
import { TopologySection } from '../sections/TopologySection';
import { StatsSection } from '../sections/StatsSection';
import { FilesSection } from '../sections/FilesSection';
import { SettingsSection } from '../sections/SettingsSection';
import { resultMessage, type SectionIntent, type WorkspaceApi } from './api';
import { OrgSwitcher } from './OrgSwitcher';
import { TabList, TabPanel, type TabItem } from './controls';
import './workspace.css';

const TABS: readonly OrgTab[] = [
  'overview',
  'members',
  'invites',
  'machines',
  'topology',
  'stats',
  'files',
  'settings',
];

/**
 * 组织 tab（第 5 节）：页头 + 8 个子页签。数据与动作只来自组织数据源；
 * 数据源未接通时：列表显示“状态未确认”，动作只在本地显示“暂不可用”，不发请求。
 */
export function OrgWorkspace({
  dispatch,
  machineModel,
  topologyModel,
  initialNotice,
}: {
  readonly dispatch: Dispatch<AppAction>;
  readonly initialNotice?: string;
  /** J6/J7 的组合模型；未接通时使用产品默认的不可用模型。 */
  readonly machineModel?: MachineSectionModel;
  readonly topologyModel?: TopologyModel;
}) {
  const domain = useOrgDomain();
  const { snapshot, gateway } = domain;
  const [viewedOrgId, setViewedOrgId] = useState<string | null>(
    () => snapshot.currentOrgId ?? snapshot.orgs?.[0]?.id ?? null,
  );
  const [tab, setTab] = useState<OrgTab>('overview');
  const [intent, setIntent] = useState<SectionIntent | null>(null);
  const [intentSeq, setIntentSeq] = useState(0);
  const [notice, setNotice] = useState<string | null>(initialNotice ?? null);
  const [askCurrent, setAskCurrent] = useState<OrgSummary | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // 已离开或不存在的组织不再作为查看对象。
  const viewedOrg =
    findOrg(snapshot, viewedOrgId) ??
    findOrg(snapshot, snapshot.currentOrgId) ??
    (snapshot.orgs ? (sortOrgs(snapshot.orgs)[0] ?? null) : null);
  const detail = orgDetail(snapshot, viewedOrg?.id);
  const perms = orgPermissions(viewedOrg, snapshot);
  const isCurrent = viewedOrg !== null && viewedOrg.id === snapshot.currentOrgId;

  // 从其他 tab 进入时（开始页提醒卡、聊天修复入口）打开指定位置。
  const { orgIntent, clearOrgIntent } = domain;
  useEffect(() => {
    if (!orgIntent) {
      return;
    }
    if (orgIntent.orgId) {
      setViewedOrgId(orgIntent.orgId);
    }
    setTab(orgIntent.tab);
    setIntent({
      segment: orgIntent.segment,
      panel: orgIntent.panel,
      prefillAccount: orgIntent.prefillAccount,
    });
    setIntentSeq((value) => value + 1);
    clearOrgIntent();
  }, [orgIntent, clearOrgIntent]);

  function goTo(next: OrgTab, nextIntent?: SectionIntent): void {
    setTab(next);
    setIntent(nextIntent ?? null);
    setIntentSeq((value) => value + 1);
  }

  async function setCurrent(orgId: string): Promise<void> {
    const target = findOrg(snapshot, orgId);
    setSwitching(true);
    const result = await gateway.setCurrentOrg(orgId);
    setSwitching(false);
    setAskCurrent(null);
    if (result.kind === 'ok' && target) {
      setNotice(ORG_COPY.currentChanged(orgDisplayName(target)));
      return;
    }
    setNotice(resultMessage(result) ?? ORG_COPY.unavailable);
  }

  async function refresh(): Promise<void> {
    setRefreshing(true);
    const result = await gateway.refresh(viewedOrg?.id ?? null);
    setRefreshing(false);
    if (result.kind !== 'ok') {
      setNotice(resultMessage(result));
    }
  }

  const api: WorkspaceApi = {
    snapshot,
    gateway,
    org: viewedOrg,
    detail,
    perms,
    isCurrent,
    intent,
    dispatch,
    notify: setNotice,
    goTo,
    openCreateOrg: () => setCreateOpen(true),
    setCurrent,
    refresh,
    refreshing,
    requestAgentView: domain.requestAgentView,
    askSetCurrent: (org) => {
      if (org.id !== snapshot.currentOrgId) {
        setAskCurrent(org);
      }
    },
  };

  const inboundPending =
    snapshot.inboundMigrations?.filter((ticket) => ticket.status === 'awaiting-target')
      .length ?? 0;
  const tabItems: readonly TabItem<OrgTab>[] = TABS.map((id) => ({
    id,
    label: ORG_TAB_LABELS[id],
    badge:
      id === 'invites'
        ? (pendingInvites(snapshot)?.length ?? undefined)
        : id === 'machines'
          ? inboundPending || undefined
          : undefined,
  }));
  const sortedOrgs = snapshot.orgs ? sortOrgs(snapshot.orgs) : [];
  // “组织是可选能力”只对还没有工作组织的人说明；已有工作组织时不占页头高度。
  const hasWorkOrg = sortedOrgs.some((org) => org.kind === 'work');

  return (
    <section
      className="page page-org org-workspace"
      data-page="org"
      data-org-mode={snapshot.status}
      data-org-viewed={viewedOrg?.id ?? ''}
    >
      <header className="org-header" data-dev-ui="DEV-UI-ORG-001">
        <OrgSwitcher
          snapshot={snapshot}
          switching={switching}
          onSwitch={(orgId) => void setCurrent(orgId)}
          onCreate={() => setCreateOpen(true)}
          onJoin={() => goTo('invites', { segment: 'join' })}
          onPendingInvites={() => goTo('invites', { segment: 'received' })}
        />
        <div className="org-header__row">
          <div className="org-header__title">
            <h1 className="page-title" id="org-page-title" tabIndex={-1}>
              {viewedOrg ? orgDisplayName(viewedOrg) : ORG_COPY.pageTitle}
            </h1>
            {viewedOrg ? (
              <div className="org-header__tags" role="group" aria-label="组织状态">
                <span className="org-tag">{ORG_KIND_LABELS[viewedOrg.kind]}</span>
                <span className="org-tag">我的角色：{ORG_ROLE_LABELS[viewedOrg.role]}</span>
                {viewedOrg.lifecycle !== 'active' ? (
                  <span className="org-tag org-tag--warning">
                    {ORG_LIFECYCLE_LABELS[viewedOrg.lifecycle]}
                  </span>
                ) : null}
                {isCurrent ? (
                  <span className="org-tag org-tag--current">{ORG_COPY.currentOrg}</span>
                ) : (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={switching}
                    onClick={() => void setCurrent(viewedOrg.id)}
                  >
                    {ORG_COPY.setCurrent}
                  </Button>
                )}
              </div>
            ) : (
              <span className="scope-chip">
                {snapshot.orgs === null ? ORG_COPY.unknown : ORG_COPY.noCurrentOrg}
              </span>
            )}
          </div>
          {sortedOrgs.length > 1 ? (
            // 5.1：组织名旁的下拉只切换查看对象，不改变当前组织。
            <div className="org-view-select">
              <label htmlFor="org-view-org">{ORG_COPY.viewOrg}</label>
              <Select
                id="org-view-org"
                className="org-view-select__trigger"
                value={viewedOrg?.id ?? ''}
                options={sortedOrgs.map((org) => ({ value: org.id, label: orgDisplayName(org) }))}
                onValueChange={(next) => {
                  setViewedOrgId(next);
                  setNotice(null);
                }}
              />
            </div>
          ) : null}
        </div>
        {hasWorkOrg ? null : <p className="page-subtitle">{ORG_COPY.pageSubtitle}</p>}
      </header>

      <div className="org-live" role="status" aria-live="polite" aria-atomic="true">
        {notice ? (
          <div className="org-live__message">
            <span>{notice}</span>
            <button
              type="button"
              className="org-live__dismiss"
              aria-label="关闭提示"
              onClick={() => setNotice(null)}
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
        ) : null}
      </div>

      {askCurrent ? (
        <Notice
          tone="info"
          title={ORG_COPY.askSetCurrent(askCurrent.name)}
          action={{
            label: ORG_COPY.setCurrent,
            onClick: () => void setCurrent(askCurrent.id),
          }}
          onDismiss={() => setAskCurrent(null)}
          dismissLabel="暂不设置"
        >
          不会自动切换；设为当前组织后，新建 Agent 默认归入它。
        </Notice>
      ) : null}

      <TabList
        items={tabItems}
        value={tab}
        onChange={(next) => {
          // 切换子页签时收起上一页的提示，避免与当前页面无关的横幅残留。
          setNotice(null);
          goTo(next);
        }}
        label="组织子页签"
        idPrefix="org-sub"
        variant="tabs"
        narrowLabel="组织页面"
      />
      <TabPanel idPrefix="org-sub" value={tab}>
        {renderSection(tab, api, intentSeq, machineModel, topologyModel)}
      </TabPanel>

      {createOpen ? (
        <CreateOrgDialog
          gateway={gateway}
          onClose={() => setCreateOpen(false)}
          onCreated={(org) => {
            setViewedOrgId(org.id);
            setTab('overview');
          }}
          onSetCurrent={(org) => void setCurrent(org.id)}
        />
      ) : null}
    </section>
  );
}

function renderSection(
  tab: OrgTab,
  api: WorkspaceApi,
  intentSeq: number,
  machineModel?: MachineSectionModel,
  topologyModel?: TopologyModel,
) {
  // 以组织与跳转序号为 key：切换查看的组织或从别处跳转时重置子页签内部状态。
  const key = `${api.org?.id ?? 'none'}:${intentSeq}`;
  switch (tab) {
    case 'overview':
      return <OverviewSection key={key} api={api} />;
    case 'members':
      return <MembersSection key={key} api={api} />;
    case 'invites':
      return <InvitesSection key={key} api={api} />;
    case 'machines':
      return <MachinesSection key={key} api={api} fallback={machineModel} />;
    case 'topology':
      return <TopologySection key={key} api={api} fallback={topologyModel} />;
    case 'stats':
      return <StatsSection key={key} api={api} />;
    case 'files':
      return <FilesSection key={key} api={api} />;
    case 'settings':
      return <SettingsSection key={key} api={api} />;
  }
}
