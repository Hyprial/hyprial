import { useEffect, useId, useRef, useState } from 'react';
import { OrgTopology } from '../../../features/network/OrgTopology';
import { buildUnavailableTopologyModel, type TopologyModel } from '../../../features/network/topology-view-model';
import type { WorkspaceApi } from '../workspace/api';
import { SectionHeader, SidePanel, TabList, TabPanel, UnknownBlock } from '../workspace/controls';
import { communicationModel, type TopologySelection, type TopologyView } from './topology-model';
import { CommunicationView, StructureGraph, communicationInspect, structureInspect } from './TopologyViews';

const VIEWS = [
  { id: 'structure' as const, label: '结构视图' },
  { id: 'communication' as const, label: '通信视图' },
];

/**
 * 拓扑（5.6）：交互沿用 J7——结构视图与通信视图切换，选择节点或边打开 Inspect 侧栏（L2）。
 * 结构视图是已采信配置里的部门、业务线、常驻角色；通信视图分 link 与路由约定两层。
 * J7 的边只表示运行时通信（传输中、已完成），表达不了这两层，所以图在本模块内画；
 * 未接通时仍用 J7 的不可用状态（交接 §0.4 接口提案）。
 */
export function TopologySection({
  api,
  fallback,
}: {
  readonly api: WorkspaceApi;
  readonly fallback?: TopologyModel;
}) {
  const { snapshot, detail } = api;
  const headingId = useId();
  const requested: TopologyView = api.intent?.segment === 'communication' ? 'communication' : 'structure';
  const [view, setView] = useState<TopologyView>(requested);
  const [selection, setSelection] = useState<TopologySelection | null>(null);
  const linksHeadingRef = useRef<HTMLHeadingElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const restoreFocus = useRef(false);

  // 从“在拓扑中查看”进入时直接打开通信视图，焦点落在第一层的标题上。
  useEffect(() => {
    if (requested === 'communication') {
      linksHeadingRef.current?.focus();
    }
  }, [requested]);

  // 关闭 Inspect 后焦点回到最后点开它的节点或边（侧栏卸载之后再移动，覆盖侧栏自己的归还）。
  useEffect(() => {
    if (selection === null && restoreFocus.current) {
      restoreFocus.current = false;
      const trigger = triggerRef.current;
      if (trigger && document.contains(trigger)) {
        trigger.focus({ preventScroll: true });
      }
    }
  }, [selection]);

  if (snapshot.status === 'unwired' || (fallback && api.org && api.detail?.config == null && api.detail?.routes == null)) {
    return (
      <div className="org-section" data-org-section="topology">
        <OrgTopology model={fallback ?? buildUnavailableTopologyModel()} />
      </div>
    );
  }

  const comm = communicationModel(detail?.agents ?? null, snapshot.agentLinks, detail?.routes ?? null);
  const config = detail?.config ?? null;
  const adopted = config?.adopted ?? null;
  const inspect = selection
    ? view === 'communication'
      ? communicationInspect(comm, selection, (agentId) => {
          api.requestAgentView({ agentId, section: 'comm' });
          api.dispatch({ type: 'open-agent-detail', agentId });
        })
      : adopted
        ? structureInspect(adopted, selection)
        : null
    : null;

  function select(next: TopologySelection, trigger: HTMLElement): void {
    triggerRef.current = trigger;
    setSelection(next);
  }

  return (
    <div className="org-section" data-org-section="topology">
      <SectionHeader
        title="组织拓扑"
        description="结构视图看组织怎样划分；通信视图看谁可以向谁发送。点节点或连线，在侧栏查看详情（Inspect）。"
        updatedAt={detail?.updatedAt}
        onRefresh={() => void api.refresh()}
        refreshing={api.refreshing}
      />
      <TabList
        items={VIEWS}
        value={view}
        onChange={(next) => {
          setView(next);
          setSelection(null);
        }}
        label="拓扑视图"
        idPrefix="org-topology"
        variant="segments"
      />
      <TabPanel idPrefix="org-topology" value={view}>
        <div className={`org-master-detail${inspect ? ' has-panel' : ''}`}>
          <div className="org-master">
            {view === 'communication' ? (
              <CommunicationView
                model={comm}
                selection={selection}
                onSelect={select}
                headingRef={linksHeadingRef}
                headingId={headingId}
              />
            ) : config === null ? (
              <UnknownBlock onRefresh={() => void api.refresh()} />
            ) : adopted ? (
              <StructureGraph adopted={adopted} selection={selection} onSelect={select} />
            ) : (
              <p className="org-empty-inline">
                还没有采信的组织配置。所有者可以在“设置 → 结构与部门”中导入或编辑。
              </p>
            )}
          </div>
          {selection && inspect ? (
            <SidePanel
              key={`${selection.type}:${selection.id}`}
              parentLabel="拓扑"
              title={inspect.title}
              onClose={() => {
                restoreFocus.current = true;
                setSelection(null);
              }}
            >
              {inspect.body}
            </SidePanel>
          ) : null}
        </div>
      </TabPanel>
    </div>
  );
}
