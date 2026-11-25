import { useEffect, useRef, useState, type Dispatch } from 'react';
import { Button, Icon, InfoTip, Notice, Select } from '../../../../components/primitives';
import type { AppAction } from '../../../../state/types';
import type { MachineSectionModel } from '../../../network';
import type { TopologyModel } from '../../../network';
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
import '../styles/org-header.css';
import '../styles/org-content.css';
import '../styles/org-dialogs.css';

const TABS: readonly OrgTab[] = [
  'overview',
  'members',
  'invites',
  'topology',
  'stats',
  'files',
  'settings',
];

/**
 * 组织 tab（第 5 节）：页头 + 8 个子页签。数据与动作只来自组织数据源；
 * 数据源没有实现的动作不渲染对应控件；动作被拒绝时由网关发出通知，本页提示条不重复显示。
 * 没有数据源（unwired）时不渲染，组织入口也已隐藏。
 */
export function OrgWorkspace({
  dispatch,
  machineModel,
  topologyModel,
  initialNotice,
}: {
  readonly dispatch: Dispatch<AppAction>;
  readonly initialNotice?: string;
  /** J6/J7 的组合模型；提供时，所选组织没有机器数据就显示它，不提供则机器页显示读取状态。 */
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
  /** 刷新失败写入的顶部提示；只清理这一来源，避免误伤加入/切换等其它提示。 */
  const refreshNoticeRef = useRef<string | null>(null);

  // 已离开或不存在的组织不再作为查看对象。
  const viewedOrg =
    findOrg(snapshot, viewedOrgId) ??
    findOrg(snapshot, snapshot.currentOrgId) ??
    (snapshot.orgs ? (sortOrgs(snapshot.orgs)[0] ?? null) : null);
  const detail = orgDetail(snapshot, viewedOrg?.id);
  const perms = orgPermissions(viewedOrg, snapshot);
  const isCurrent = viewedOrg !== null && viewedOrg.id === snapshot.currentOrgId;

  // Switching the viewed org only changes local state; load that org's
  // read-only detail explicitly.  The list/current-org selection and
  // org.fetch are untouched.  A missing detail keeps the section's
  // refresh/unknown block until this read settles.
  useEffect(() => {
    const orgId = viewedOrg?.id;
    if (!orgId || detail !== null) return;
    void gateway.loadOrgDetail(orgId).then((result) => {
      const message = resultMessage(result);
      if (message !== null) setNotice(message);
    });
  }, [viewedOrg?.id, detail, gateway]);

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

  const setCurrentOrg = gateway.setCurrentOrg?.bind(gateway);
  async function setCurrent(orgId: string): Promise<void> {
    if (!setCurrentOrg) return;
    const target = findOrg(snapshot, orgId);
    setSwitching(true);
    const result = await setCurrentOrg(orgId);
    setSwitching(false);
    if (result.kind === 'unavailable') return;
    setAskCurrent(null);
    if (result.kind === 'ok' && target) {
      setViewedOrgId(target.id);
      setIntentSeq((value) => value + 1);
      setNotice(ORG_COPY.currentChanged(orgDisplayName(target)));
      return;
    }
    setNotice(resultMessage(result));
  }

  async function refresh(): Promise<void> {
    setRefreshing(true);
    const result = await gateway.refresh(viewedOrg?.id ?? null);
    setRefreshing(false);
    if (result.kind === 'unavailable') return;
    if (result.kind !== 'ok') {
      const message = resultMessage(result);
      refreshNoticeRef.current = message;
      setNotice(message);
      return;
    }
    // 后续刷新成功时清掉本组件上次刷新失败留下的提示；其它来源的提示保持不变。
    const refreshNotice = refreshNoticeRef.current;
    refreshNoticeRef.current = null;
    if (refreshNotice !== null) {
      setNotice((current) => (current === refreshNotice ? null : current));
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
    guard: domain.guard,
    refusal: domain.refusal,
    tabAvailable: domain.tabShown,
    labelled: domain.labelled,
    openCreateOrg: () => {
      if (domain.guard(ORG_COPY.createOrg, 'createOrg')) setCreateOpen(true);
    },
    ...(setCurrentOrg ? { setCurrent } : {}),
    refresh,
    refreshing,
    requestAgentView: domain.requestAgentView,
    askSetCurrent: (org) => {
      if (setCurrentOrg && org.id !== snapshot.currentOrgId) {
        setAskCurrent(org);
      }
    },
  };

  const visibleTabs = TABS.filter((id) => domain.tabShown(id));
  const activeTab = visibleTabs.includes(tab) ? tab : 'overview';
  const tabItems: readonly TabItem<OrgTab>[] = visibleTabs.map((id) => ({
    id,
    label: ORG_TAB_LABELS[id],
    badge:
      id === 'invites' && gateway.claimInvite !== undefined
        ? (pendingInvites(snapshot)?.length ?? undefined)
        : undefined,
  }));
  const sortedOrgs = snapshot.orgs ? sortOrgs(snapshot.orgs) : [];
  const hasWorkOrg = sortedOrgs.length > 0;

  if (snapshot.status === 'unwired') return null;

  return (
    <section
      className="page page-org org-workspace"
      data-page="org"
      data-org-mode={snapshot.status}
      data-org-viewed={viewedOrg?.id ?? ''}
    >
      <header className="org-header" data-dev-ui="DEV-UI-ORG-001">
        <div className="org-header__row">
          <div className="org-header__title">
            <h1 className="page-title" id="org-page-title" tabIndex={-1}>
              {viewedOrg ? orgDisplayName(viewedOrg) : ORG_COPY.pageTitle}
            </h1>
            {hasWorkOrg ? null : <InfoTip content={ORG_COPY.pageSubtitle} label="组织说明" />}
            {viewedOrg ? (
              <div className="org-header__tags" role="group" aria-label="组织状态">
                <span className="org-tag">{ORG_KIND_LABELS[viewedOrg.kind]}</span>
                <span className="org-tag">我的角色：{viewedOrg.role ? ORG_ROLE_LABELS[viewedOrg.role] : ORG_COPY.unknown}</span>
                {viewedOrg.lifecycle !== 'active' ? (
                  <span className="org-tag org-tag--warning">
                    {ORG_LIFECYCLE_LABELS[viewedOrg.lifecycle]}
                  </span>
                ) : null}
                {isCurrent ? (
                  <span className="org-tag org-tag--current">{ORG_COPY.currentOrg}</span>
                ) : setCurrentOrg ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={switching}
                    onClick={() => void setCurrent(viewedOrg.id)}
                  >
                    {ORG_COPY.setCurrent}
                  </Button>
                ) : null}
              </div>
            ) : null}
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
                  // 显式切换查看对象时重置子页签内部状态；列表刷新带来的
                  // none→新组织切换不走这里，加入结果因此不会被卸载。
                  setIntentSeq((value) => value + 1);
                  setNotice(null);
                }}
              />
            </div>
          ) : null}
        </div>
        <OrgSwitcher
          snapshot={snapshot}
          showPendingInvites={gateway.claimInvite !== undefined}
          switching={switching}
          onSwitch={setCurrentOrg ? (orgId) => void setCurrent(orgId) : undefined}
          onCreate={api.openCreateOrg}
          onJoin={() => goTo('invites', { segment: 'join' })}
          onPendingInvites={() => goTo('invites', { segment: 'received' })}
        />
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
              <Icon name="x" size="sm" />
            </button>
          </div>
        ) : null}
      </div>

      {askCurrent && setCurrentOrg ? (
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
          设为当前组织后，新建的 Agent 默认归入它。
        </Notice>
      ) : null}

      <TabList
        items={tabItems}
        value={activeTab}
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
      <TabPanel idPrefix="org-sub" value={activeTab}>
        {renderSection(activeTab, api, intentSeq, machineModel, topologyModel)}
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
  // 邀请页的加入结果必须跨 org.list 刷新存活：首次加入会同时把 viewedOrg 从
  // none 变成新组织，不能因此重建 JoinByLink。显式切换组织/页签仍通过
  // intentSeq 重置（见下拉 onValueChange 与 goTo）。
  const invitesKey = `invites:${intentSeq}`;
  switch (tab) {
    case 'overview':
      return <OverviewSection key={key} api={api} />;
    case 'members':
      return <MembersSection key={key} api={api} fallback={machineModel} />;
    case 'invites':
      return <InvitesSection key={invitesKey} api={api} />;
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
