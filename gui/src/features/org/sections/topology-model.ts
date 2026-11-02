/**
 * 拓扑两种视图的展示模型（交互设计 5.6、UJ09 第 2 步）。只投影数据源已有的内容，不推断：
 * - 通信视图分两层：link（本机 Agent → 对端，带方向）与路由约定（组织配置里的静态约定）。
 *   路由约定只写明发往的对象，“范围”的语义尚未定义，所以不画方向。
 * - 结构视图：本节点已采信配置里的部门汇报关系、业务线、常驻角色。
 * `null` 表示来源未提供，界面显示“状态未确认”，不能当作没有。
 */
import { ORG_COPY } from '../domain/copy';
import type {
  AgentLink,
  OrgAgentRecord,
  OrgBusinessLine,
  OrgConfig,
  OrgDepartment,
  OrgResidentRole,
  OrgRoute,
} from '../domain/types';

export type TopologyView = 'structure' | 'communication';

/** 选中的节点或边；id 在同一视图内唯一，同一节点出现多处时共用一个 id。 */
export interface TopologySelection {
  readonly type: 'node' | 'edge';
  readonly id: string;
}

export const LINK_STATE_LABELS: Readonly<Record<AgentLink['state'], string>> = {
  active: '可用',
  unknown: ORG_COPY.unknown,
};

export interface LinkEdge {
  readonly id: string;
  readonly fromId: string;
  readonly toId: string;
  readonly holder: OrgAgentRecord;
  readonly link: AgentLink;
}

export interface HolderGroup {
  readonly nodeId: string;
  readonly agent: OrgAgentRecord;
  readonly edges: readonly LinkEdge[];
}

export interface RouteEdge {
  readonly id: string;
  /** 路由对象（发往的部门、业务线等）。 */
  readonly fromId: string;
  /** 范围，按配置原文。 */
  readonly toId: string;
  readonly route: OrgRoute;
}

export interface CommunicationModel {
  /** 这个组织里的本机 Agent 与它们持有的 link；Agent 或 link 来源未提供时为 null。 */
  readonly holders: readonly HolderGroup[] | null;
  readonly routes: readonly RouteEdge[] | null;
}

export const agentNodeId = (agentId: string): string => `agent:${agentId}`;
export const peerNodeId = (peerLabel: string): string => `peer:${peerLabel}`;
export const routeTargetNodeId = (route: OrgRoute): string => `target:${route.kind}:${route.target}`;
export const routeScopeNodeId = (route: OrgRoute): string => `scope:${route.scope}`;

export function communicationModel(
  agents: readonly OrgAgentRecord[] | null,
  links: Readonly<Record<string, readonly AgentLink[]>> | null,
  routes: readonly OrgRoute[] | null,
): CommunicationModel {
  // 只画这个组织里的本机 Agent；link 只含本机 Agent 持有的（WP2 可用前的数据范围）。
  const mine = agents?.filter((agent) => agent.mine) ?? null;
  return {
    holders:
      mine && links
        ? mine.map((agent) => ({
            nodeId: agentNodeId(agent.id),
            agent,
            edges: (links[agent.id] ?? []).map((link) => ({
              id: `link:${link.id}`,
              fromId: agentNodeId(agent.id),
              toId: peerNodeId(link.peerLabel),
              holder: agent,
              link,
            })),
          }))
        : null,
    routes: routes
      ? routes.map((route) => ({
          id: `route:${route.id}`,
          fromId: routeTargetNodeId(route),
          toId: routeScopeNodeId(route),
          route,
        }))
      : null,
  };
}

export function linkEdges(model: CommunicationModel): readonly LinkEdge[] {
  return model.holders?.flatMap((group) => group.edges) ?? [];
}

/** 选中项相连的节点与边：选节点时标出它的边，选边时标出两端。 */
export function relatedIds(
  selection: TopologySelection | null,
  edges: readonly { readonly id: string; readonly fromId: string; readonly toId: string }[],
): ReadonlySet<string> {
  const related = new Set<string>();
  if (!selection) return related;
  for (const edge of edges) {
    if (selection.type === 'node' && (edge.fromId === selection.id || edge.toId === selection.id)) {
      related.add(edge.id);
    }
    if (selection.type === 'edge' && edge.id === selection.id) {
      related.add(edge.fromId);
      related.add(edge.toId);
    }
  }
  return related;
}

export interface DepartmentNode {
  readonly dept: OrgDepartment;
  readonly children: readonly DepartmentNode[];
}

export const deptNodeId = (dept: OrgDepartment): string => `dept:${dept.id}`;
export const lineNodeId = (line: OrgBusinessLine): string => `line:${line.id}`;
export const roleNodeId = (role: OrgResidentRole): string => `role:${role.id}`;
export const roleLabel = (role: OrgResidentRole): string => `${role.node}（${role.role}）`;

/**
 * 部门按汇报关系分层：没有上级（或上级不存在）的是第一层。
 * 配置里出现循环时，未挂上的部门也作为第一层列出，不丢部门；校验会报告这类错误。
 */
export function departmentTree(departments: readonly OrgDepartment[]): readonly DepartmentNode[] {
  const ids = new Set(departments.map((dept) => dept.id));
  const children = new Map<string, OrgDepartment[]>();
  const roots: OrgDepartment[] = [];
  for (const dept of departments) {
    if (dept.parent && dept.parent !== dept.id && ids.has(dept.parent)) {
      children.set(dept.parent, [...(children.get(dept.parent) ?? []), dept]);
    } else {
      roots.push(dept);
    }
  }
  const placed = new Set<string>();
  const visit = (dept: OrgDepartment): DepartmentNode => {
    placed.add(dept.id);
    return {
      dept,
      children: (children.get(dept.id) ?? []).filter((child) => !placed.has(child.id)).map(visit),
    };
  };
  const tree = roots.map(visit);
  for (const dept of departments) {
    if (!placed.has(dept.id)) tree.push(visit(dept));
  }
  return tree;
}

export function findDepartment(config: OrgConfig, id: string | null): OrgDepartment | null {
  return config.departments.find((dept) => dept.id === id) ?? null;
}
