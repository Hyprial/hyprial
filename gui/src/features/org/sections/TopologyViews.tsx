import type { MouseEvent, ReactNode, RefObject } from 'react';
import { Button } from '../../../components/primitives';
import { ORG_COPY, zh } from '../domain/copy';
import type { ConfigVersion } from '../domain/types';
import { UnknownBlock } from '../workspace/controls';
import {
  LINK_STATE_LABELS,
  deptNodeId,
  departmentTree,
  findDepartment,
  lineNodeId,
  linkEdges,
  relatedIds,
  roleLabel,
  roleNodeId,
  routeTargetNodeId,
  type CommunicationModel,
  type DepartmentNode,
  type TopologySelection,
} from './topology-model';
import './topology-views.css';

type Select = (selection: TopologySelection, trigger: HTMLElement) => void;

/** 结构视图不标相连项。 */
const NONE: ReadonlySet<string> = new Set();

/** 图中的一个节点。同一节点出现多处时共用 id，选中时各处一起标出。 */
function GraphNode({
  id,
  kind,
  label,
  role,
  meta,
  selection,
  related,
  onSelect,
}: {
  readonly id: string;
  readonly kind: 'agent' | 'peer' | 'target' | 'scope' | 'dept' | 'line' | 'role';
  readonly label: string;
  /** 读屏用的节点类型，例如“本机 Agent”“部门”。 */
  readonly role: string;
  readonly meta?: string;
  readonly selection: TopologySelection | null;
  readonly related: ReadonlySet<string>;
  readonly onSelect: Select;
}) {
  return (
    <button
      type="button"
      className={`org-graph__node org-graph__node--${kind}`}
      data-graph-node={id}
      aria-pressed={selection?.type === 'node' && selection.id === id}
      data-related={related.has(id) || undefined}
      aria-label={[label, role, meta].filter(Boolean).join('，')}
      onClick={(event: MouseEvent<HTMLButtonElement>) => onSelect({ type: 'node', id }, event.currentTarget)}
    >
      <span className="org-graph__node-label">{label}</span>
      <span className="org-graph__node-meta">{meta ? `${role} · ${meta}` : role}</span>
    </button>
  );
}

/** 图中的一条边：link 是带箭头的实线，路由约定是不带箭头的虚线。 */
function GraphEdge({
  id,
  layer,
  text,
  name,
  state,
  selection,
  related,
  onSelect,
}: {
  readonly id: string;
  readonly layer: 'link' | 'route';
  readonly text: string;
  readonly name: string;
  readonly state?: string;
  readonly selection: TopologySelection | null;
  readonly related: ReadonlySet<string>;
  readonly onSelect: Select;
}) {
  return (
    <button
      type="button"
      className={`org-graph__edge org-graph__edge--${layer}`}
      data-graph-edge={id}
      data-state={state}
      aria-pressed={selection?.type === 'edge' && selection.id === id}
      data-related={related.has(id) || undefined}
      aria-label={name}
      onClick={(event: MouseEvent<HTMLButtonElement>) => onSelect({ type: 'edge', id }, event.currentTarget)}
    >
      <span className="org-graph__line" aria-hidden="true" />
      <span className="org-graph__edge-label">{text}</span>
      <span className="org-graph__line" aria-hidden="true" />
      {layer === 'link' ? <span className="org-graph__arrow" aria-hidden="true" /> : null}
    </button>
  );
}

/**
 * 通信视图（5.6）：link 与路由约定分两层，用不同图例，不合并成一个“可以通信”的结论。
 * link 只含本机 Agent 持有的（WP2 可用前），按发送方分组：发送方 → 接收方。
 */
export function CommunicationView({
  model,
  selection,
  onSelect,
  headingRef,
  headingId,
}: {
  readonly model: CommunicationModel;
  readonly selection: TopologySelection | null;
  readonly onSelect: Select;
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
  readonly headingId: string;
}) {
  const related = relatedIds(selection, [...linkEdges(model), ...(model.routes ?? [])]);
  const anyLink = model.holders?.some((group) => group.edges.length > 0) ?? false;
  return (
    <div className="org-topology-view" data-dev-ui="DEV-UI-ORG-010">
      <p className="org-scope-note">
        两层分开判断：路由约定是组织配置里的静态约定；link 表示发送方持有指向接收方的链接，只有持有方可以发送。
      </p>
      <ul className="org-graph-legend" aria-label="图例">
        <li>
          <span className="org-graph-legend__swatch org-graph-legend__swatch--link" aria-hidden="true" />
          实线箭头：link，发送方 → 接收方
        </li>
        <li>
          <span className="org-graph-legend__swatch org-graph-legend__swatch--route" aria-hidden="true" />
          虚线：路由约定，静态，不画方向
        </li>
        <li>胶囊：Agent；方框：路由对象与范围</li>
      </ul>

      <section className="org-graph-layer" aria-labelledby={`${headingId}-links`}>
        <h3 id={`${headingId}-links`} ref={headingRef} tabIndex={-1} className="org-subheading">
          link（发送方 → 接收方）
        </h3>
        <p className="org-muted">{ORG_COPY.linksLocalOnly}</p>
        {model.holders === null ? (
          <UnknownBlock />
        ) : model.holders.length === 0 ? (
          <p className="org-empty-inline">这个组织里还没有本机 Agent。</p>
        ) : !anyLink ? (
          <p className="org-empty-inline">本机 Agent 还没有 link。</p>
        ) : (
          <ul className="org-graph" aria-label="link 关系图">
            {model.holders.map((group) => (
              <li key={group.nodeId} className="org-graph__group">
                <div className={`org-graph__holder${group.edges.length > 0 ? ' has-edges' : ''}`}>
                  <GraphNode
                    id={group.nodeId}
                    kind="agent"
                    label={group.agent.name}
                    role="本机 Agent"
                    meta={group.edges.length > 0 ? `${group.edges.length} 条 link` : '还没有 link'}
                    selection={selection}
                    related={related}
                    onSelect={onSelect}
                  />
                </div>
                {group.edges.length > 0 ? (
                  <ul className="org-graph__out" aria-label={`${group.agent.name} 持有的 link`}>
                    {group.edges.map((edge) => {
                      const state = LINK_STATE_LABELS[edge.link.state];
                      return (
                        <li key={edge.id} className="org-graph__row">
                          <GraphEdge
                            id={edge.id}
                            layer="link"
                            text={state}
                            name={zh`link：${group.agent.name} 发送给 ${edge.link.peerLabel}，${state}`}
                            state={edge.link.state}
                            selection={selection}
                            related={related}
                            onSelect={onSelect}
                          />
                          <GraphNode
                            id={edge.toId}
                            kind="peer"
                            label={edge.link.peerLabel}
                            role="接收方"
                            selection={selection}
                            related={related}
                            onSelect={onSelect}
                          />
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="org-graph-layer" aria-labelledby={`${headingId}-routes`}>
        <h3 id={`${headingId}-routes`} className="org-subheading">
          路由约定（静态）
        </h3>
        <p className="org-muted">来自本节点已采信的组织配置；范围的语义尚未定义，按原文显示。</p>
        {model.routes === null ? (
          <UnknownBlock />
        ) : model.routes.length === 0 ? (
          <p className="org-empty-inline">没有路由约定。</p>
        ) : (
          <ul className="org-graph" aria-label="路由约定关系图">
            {model.routes.map((edge) => (
              <li key={edge.id} className="org-graph__route">
                <div className="org-graph__holder org-graph__holder--route has-edges">
                  <GraphNode
                    id={edge.fromId}
                    kind="target"
                    label={edge.route.target}
                    role={`路由对象（${edge.route.kind}）`}
                    selection={selection}
                    related={related}
                    onSelect={onSelect}
                  />
                </div>
                <GraphEdge
                  id={edge.id}
                  layer="route"
                  text="路由约定"
                  name={`路由约定：${edge.route.kind} · ${edge.route.target}，范围：${edge.route.scope}`}
                  selection={selection}
                  related={related}
                  onSelect={onSelect}
                />
                <GraphNode
                  id={edge.toId}
                  kind="scope"
                  label={edge.route.scope}
                  role="范围（原文）"
                  selection={selection}
                  related={related}
                  onSelect={onSelect}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function DepartmentItem({
  node,
  selection,
  onSelect,
}: {
  readonly node: DepartmentNode;
  readonly selection: TopologySelection | null;
  readonly onSelect: Select;
}) {
  const { dept } = node;
  return (
    <li>
      <GraphNode
        id={deptNodeId(dept)}
        kind="dept"
        label={dept.name || '未命名部门'}
        role="部门"
        meta={`负责人 ${dept.lead || '未设置'}`}
        selection={selection}
        related={NONE}
        onSelect={onSelect}
      />
      {node.children.length > 0 ? (
        <ul aria-label={`${dept.name || '未命名部门'} 的下级部门`}>
          {node.children.map((child) => (
            <DepartmentItem key={child.dept.id} node={child} selection={selection} onSelect={onSelect} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** 结构视图（5.6）：本节点已采信配置里的部门汇报关系（缩进的是下级）、业务线、常驻角色。 */
export function StructureGraph({
  adopted,
  selection,
  onSelect,
}: {
  readonly adopted: ConfigVersion;
  readonly selection: TopologySelection | null;
  readonly onSelect: Select;
}) {
  const { config } = adopted;
  return (
    <div className="org-topology-view" data-dev-ui="DEV-UI-ORG-019">
      <p className="org-muted">
        来自本节点已采信的组织配置（版本 {adopted.version}）。部门之间的竖线是汇报关系，缩进的是下级。
      </p>
      <section className="org-graph-layer" aria-label="部门与汇报关系">
        <h3 className="org-subheading">部门与汇报关系</h3>
        {config.departments.length === 0 ? (
          <p className="org-empty-inline">还没有部门。</p>
        ) : (
          <ul className="org-graph-tree" aria-label="部门与汇报关系">
            {departmentTree(config.departments).map((node) => (
              <DepartmentItem key={node.dept.id} node={node} selection={selection} onSelect={onSelect} />
            ))}
          </ul>
        )}
      </section>
      <section className="org-graph-layer" aria-label="业务线">
        <h3 className="org-subheading">业务线</h3>
        {config.lines.length === 0 ? (
          <p className="org-empty-inline">还没有业务线。</p>
        ) : (
          <ul className="org-graph-nodes" aria-label="业务线">
            {config.lines.map((line) => (
              <li key={line.id}>
                <GraphNode
                  id={lineNodeId(line)}
                  kind="line"
                  label={line.name}
                  role="业务线"
                  meta={`负责人 ${line.lead || '未设置'}`}
                  selection={selection}
                  related={NONE}
                  onSelect={onSelect}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="org-graph-layer" aria-label="常驻角色">
        <h3 className="org-subheading">常驻角色</h3>
        {config.roles.length === 0 ? (
          <p className="org-empty-inline">还没有常驻角色。</p>
        ) : (
          <ul className="org-graph-nodes" aria-label="常驻角色">
            {config.roles.map((role) => (
              <li key={role.id}>
                <GraphNode
                  id={roleNodeId(role)}
                  kind="role"
                  label={roleLabel(role)}
                  role="常驻角色"
                  meta={`服务 ${role.serves || '未设置'}`}
                  selection={selection}
                  related={NONE}
                  onSelect={onSelect}
                />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function Facts({ items }: { readonly items: readonly (readonly [string, ReactNode])[] }) {
  return (
    <dl className="org-facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export interface InspectContent {
  readonly title: string;
  readonly body: ReactNode;
}

/** 通信视图的 Inspect：边写明层、两端、方向与状态；节点列出它的边。 */
export function communicationInspect(
  model: CommunicationModel,
  selection: TopologySelection,
  onManageLinks: (agentId: string) => void,
): InspectContent | null {
  const edges = linkEdges(model);
  const routes = model.routes ?? [];
  if (selection.type === 'edge') {
    const edge = edges.find((item) => item.id === selection.id);
    if (edge) {
      const holder = edge.holder.name;
      const peer = edge.link.peerLabel;
      return {
        title: `Inspect：${holder} → ${peer}`,
        body: (
          <>
            <Facts
              items={[
                ['层', 'link：发送方持有指向接收方的链接'],
                ['发送方', `${holder}（本机 Agent）`],
                ['接收方', peer],
                ['方向', zh`单向：${holder} 可以向 ${peer} 发送；反方向要看 ${peer} 是否持有指向 ${holder} 的 link。`],
                ['状态', LINK_STATE_LABELS[edge.link.state]],
                ['在途', `${edge.link.inFlight} 条`],
                ['最近使用', edge.link.lastUsedAt ?? '暂无'],
                ['数据范围', ORG_COPY.linksLocalOnly],
              ]}
            />
            <div className="org-actions">
              <Button size="sm" onClick={() => onManageLinks(edge.holder.id)}>
                在 Agent 详情中管理
              </Button>
            </div>
          </>
        ),
      };
    }
    const route = routes.find((item) => item.id === selection.id)?.route;
    if (route) {
      return {
        title: `Inspect：路由约定（${route.kind} · ${route.target}）`,
        body: (
          <>
            <Facts
              items={[
                ['层', '路由约定：组织配置里的静态约定'],
                ['种类', route.kind],
                ['对象', route.target],
                ['范围', `${route.scope}（范围的语义尚未定义，按原文显示）`],
                ['方向', '未定义'],
              ]}
            />
            <p className="org-muted">
              配置写明了发往的对象；“范围”的语义尚未定义，所以不画方向。路由约定不等于可以发送：还要双方共享组织，且发送方持有 link。
            </p>
          </>
        ),
      };
    }
    return null;
  }

  const holder = model.holders?.find((group) => group.nodeId === selection.id);
  if (holder) {
    return {
      title: `Inspect：${holder.agent.name}`,
      body: (
        <>
          <Facts
            items={[
              ['类型', '本机 Agent'],
              ['所在机器', holder.agent.machineLabel],
            ]}
          />
          <h4 className="org-subheading">持有的 link</h4>
          {holder.edges.length === 0 ? (
            <p className="org-empty-inline">还没有 link。</p>
          ) : (
            <ul className="org-rows" aria-label="持有的 link">
              {holder.edges.map((edge) => (
                <li key={edge.id} className="org-row">
                  {`${holder.agent.name} → ${edge.link.peerLabel} · ${LINK_STATE_LABELS[edge.link.state]}`}
                </li>
              ))}
            </ul>
          )}
          <p className="org-muted">{zh`别人持有、指向 ${holder.agent.name} 的 link 不在本机数据范围。`}</p>
          <div className="org-actions">
            <Button size="sm" onClick={() => onManageLinks(holder.agent.id)}>
              在 Agent 详情中管理
            </Button>
          </div>
        </>
      ),
    };
  }
  const incoming = edges.filter((edge) => edge.toId === selection.id);
  if (incoming.length > 0) {
    const peer = incoming[0].link.peerLabel;
    return {
      title: `Inspect：${peer}`,
      body: (
        <>
          <Facts items={[['类型', 'link 接收方']]} />
          <h4 className="org-subheading">收到的 link</h4>
          <ul className="org-rows" aria-label="收到的 link">
            {incoming.map((edge) => (
              <li key={edge.id} className="org-row">
                {`${edge.holder.name} → ${peer} · ${LINK_STATE_LABELS[edge.link.state]}`}
              </li>
            ))}
          </ul>
          <p className="org-muted">它发出的 link 不在本机数据范围（{ORG_COPY.linksLocalOnly}）。</p>
        </>
      ),
    };
  }
  const touching = routes.filter((edge) => edge.fromId === selection.id || edge.toId === selection.id);
  if (touching.length > 0) {
    const asTarget = routeTargetNodeId(touching[0].route) === selection.id;
    const label = asTarget ? touching[0].route.target : touching[0].route.scope;
    return {
      title: `Inspect：${label}`,
      body: (
        <>
          <Facts items={[['类型', asTarget ? `路由对象（${touching[0].route.kind}）` : '路由范围（原文）']]} />
          <h4 className="org-subheading">相关的路由约定</h4>
          <ul className="org-rows" aria-label="相关的路由约定">
            {touching.map((edge) => (
              <li key={edge.id} className="org-row">
                {`${edge.route.kind} · ${edge.route.target} · 范围：${edge.route.scope}`}
              </li>
            ))}
          </ul>
        </>
      ),
    };
  }
  return null;
}

/** 结构视图的 Inspect：部门写明负责人、上下级与成员；业务线、常驻角色列出配置字段。 */
export function structureInspect(adopted: ConfigVersion, selection: TopologySelection): InspectContent | null {
  const { config } = adopted;
  const dept = config.departments.find((item) => deptNodeId(item) === selection.id);
  if (dept) {
    const children = config.departments.filter((item) => item.parent === dept.id);
    return {
      title: `Inspect：${dept.name || '未命名部门'}`,
      body: (
        <Facts
          items={[
            ['负责人', dept.lead || '未设置'],
            ['上级部门', findDepartment(config, dept.parent)?.name ?? '—'],
            ['下级部门', children.map((item) => item.name).join('、') || '—'],
            ['成员', dept.members.join('、') || '—'],
          ]}
        />
      ),
    };
  }
  const line = config.lines.find((item) => lineNodeId(item) === selection.id);
  if (line) {
    return {
      title: `Inspect：${line.name}`,
      body: (
        <Facts
          items={[
            ['负责人', line.lead || '未设置'],
            ['协调人', line.coordinator || '未设置'],
            ['看板', line.board || '未设置'],
            ['代码仓库', line.repo || '未设置'],
          ]}
        />
      ),
    };
  }
  const role = config.roles.find((item) => roleNodeId(item) === selection.id);
  if (role) {
    return {
      title: `Inspect：${roleLabel(role)}`,
      body: (
        <Facts
          items={[
            ['节点', role.node],
            ['角色', role.role],
            ['服务对象', role.serves || '未设置'],
          ]}
        />
      ),
    };
  }
  return null;
}
