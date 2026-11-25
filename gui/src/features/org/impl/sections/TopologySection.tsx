import { useEffect, useId, useRef, useState } from 'react';
import { OrgTopology, type TopologyModel } from '../../../network';
import type { WorkspaceApi } from '../workspace/api';
import {
  OrgReadStateBlock,
  SectionHeader,
  SidePanel,
  TabList,
  TabPanel,
  UnknownBlock,
} from '../workspace/controls';
import { communicationModel, type TopologySelection, type TopologyView } from './topology-model';
import { CommunicationView, StructureGraph, communicationInspect, structureInspect } from './TopologyViews';
import { NetworkStatusSection } from './NetworkStatusSection';

type SectionView = TopologyView | 'network';

const VIEWS = [
  { id: 'structure' as const, label: '结构视图' },
  { id: 'communication' as const, label: '通信视图' },
  { id: 'network' as const, label: '网络状态' },
];

/**
 * 拓扑（5.6）：交互沿用 J7——结构视图与通信视图切换，选择节点或边打开 Inspect 侧栏（L2）。
 * 结构视图是已采信配置里的部门、业务线、常驻角色；通信视图分 link 与路由约定两层。
 * J7 的边只表示运行时通信（传输中、已完成），表达不了这两层，所以图在本模块内画；
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
  const [view, setView] = useState<SectionView>(requested);
  const [selection, setSelection] = useState<TopologySelection | null>(null);
  const routesHeadingRef = useRef<HTMLHeadingElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const restoreFocus = useRef(false);

  // 从“在拓扑中查看”进入时直接打开通信视图，焦点落在第一层的标题上。
  useEffect(() => {
    if (requested === 'communication') {
      routesHeadingRef.current?.focus();
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

  if (snapshot.status === 'loading' || snapshot.status === 'error') {
    return (
      <div className="org-section" data-org-section="topology">
        <SectionHeader title="组织拓扑" />
        <OrgReadStateBlock snapshot={snapshot} onRefresh={() => void api.refresh()} />
      </div>
    );
  }

  if (fallback && api.org && api.detail?.config == null && api.detail?.routes == null) {
    return (
      <div className="org-section" data-org-section="topology">
        <OrgTopology model={fallback} />
      </div>
    );
  }

  const comm = communicationModel(detail?.routes ?? null);
  const config = detail?.config ?? null;
  const adopted = config?.adopted ?? null;
  const inspect = selection
    ? view === 'communication'
      ? communicationInspect(comm, selection)
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
        description="点节点或连线查看详情。"
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
        {view === 'network' ? <NetworkStatusSection api={api} /> : (
        <div className={`org-master-detail${inspect ? ' has-panel' : ''}`}>
          <div className="org-master">
            {view === 'communication' ? (
              <CommunicationView
                model={comm}
                selection={selection}
                onSelect={select}
                headingRef={routesHeadingRef}
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
        )}
      </TabPanel>
    </div>
  );
}
