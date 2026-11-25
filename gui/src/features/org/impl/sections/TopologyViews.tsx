import type { MouseEvent, ReactNode, RefObject } from 'react';
import type { ConfigVersion } from '../domain/types';
import { UnknownBlock } from '../workspace/controls';
import {
  deptNodeId,
  departmentTree,
  findDepartment,
  lineNodeId,
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
  readonly kind: 'target' | 'scope' | 'dept' | 'line' | 'role';
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

/** 图中的一条边：路由约定是不带箭头的虚线。 */
function GraphEdge({
  id,
  layer,
  text,
  name,
  selection,
  related,
  onSelect,
}: {
  readonly id: string;
  readonly layer: 'route';
  readonly text: string;
  readonly name: string;
  readonly selection: TopologySelection | null;
  readonly related: ReadonlySet<string>;
  readonly onSelect: Select;
}) {
  return (
    <button
      type="button"
      className={`org-graph__edge org-graph__edge--${layer}`}
      data-graph-edge={id}
      aria-pressed={selection?.type === 'edge' && selection.id === id}
      data-related={related.has(id) || undefined}
      aria-label={name}
      onClick={(event: MouseEvent<HTMLButtonElement>) => onSelect({ type: 'edge', id }, event.currentTarget)}
    >
      <span className="org-graph__line" aria-hidden="true" />
      <span className="org-graph__edge-label">{text}</span>
      <span className="org-graph__line" aria-hidden="true" />
    </button>
  );
}

/**
 * 通信视图（5.6）：显示组织配置里的静态路由约定。
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
  const related = relatedIds(selection, model.routes ?? []);
  return (
    <div className="org-topology-view" data-dev-ui="DEV-UI-ORG-010">
      <p className="org-scope-note">
        路由约定只是组织配置里的静态约定，按原文显示。
      </p>
      <ul className="org-graph-legend" aria-label="图例">
        <li>
          <span className="org-graph-legend__swatch org-graph-legend__swatch--route" aria-hidden="true" />
          虚线：路由约定，静态，不画方向
        </li>
        <li>方框：路由对象与范围</li>
      </ul>

      <section className="org-graph-layer" aria-labelledby={`${headingId}-routes`}>
        <h3 id={`${headingId}-routes`} ref={headingRef} tabIndex={-1} className="org-subheading">
          路由约定（静态）
        </h3>
        <p className="org-muted">来自已采信的组织配置，按原文显示。</p>
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
        来自本节点已采信的组织配置（版本 {adopted.version}）。
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

/** 通信视图的 Inspect：路由约定写明对象与范围；节点列出相关的路由约定。 */
export function communicationInspect(
  model: CommunicationModel,
  selection: TopologySelection,
): InspectContent | null {
  const routes = model.routes ?? [];
  if (selection.type === 'edge') {
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
                ['范围', route.scope],
                ['方向', '未定义'],
              ]}
            />
            <p className="org-muted">
              路由约定只是组织配置里的静态约定，不代表可以发送。
            </p>
          </>
        ),
      };
    }
    return null;
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
