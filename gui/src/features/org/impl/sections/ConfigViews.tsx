import type { OrgConfig, OrgRoute } from '../domain/types';

/** 只读表格：宽屏是表格，窄屏改成每行一组“字段名：值”（第 12 节，单元格带 data-label）。 */
function StackTable({
  caption,
  columns,
  rows,
}: {
  readonly caption?: string;
  readonly columns: readonly string[];
  readonly rows: readonly { readonly id: string; readonly cells: readonly string[] }[];
}) {
  return (
    <table className="org-table org-table--stack">
      {caption ? <caption>{caption}</caption> : null}
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column} scope="col">
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            {row.cells.map((cell, index) => (
              <td key={columns[index]} data-label={columns[index]}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 已采信或待导入配置的只读展示：部门与汇报关系、业务线、常驻角色。 */
export function StructureView({ config }: { readonly config: OrgConfig }) {
  return (
    <div className="org-structure">
      <StackTable
        caption="部门与汇报关系"
        columns={['部门', '负责人', '上级部门', '成员']}
        rows={config.departments.map((dept) => ({
          id: dept.id,
          cells: [
            dept.name,
            dept.lead,
            config.departments.find((item) => item.id === dept.parent)?.name ?? '—',
            dept.members.join('、') || '—',
          ],
        }))}
      />
      <StackTable
        caption="业务线"
        columns={['业务线', '负责人', '协调人', '看板', '代码仓库']}
        rows={config.lines.map((line) => ({
          id: line.id,
          cells: [line.name, line.lead, line.coordinator, line.board, line.repo],
        }))}
      />
      <StackTable
        caption="常驻角色"
        columns={['节点', '角色', '服务对象']}
        rows={config.roles.map((role) => ({ id: role.id, cells: [role.node, role.role, role.serves] }))}
      />
    </div>
  );
}

/** 路由约定（组织配置中的静态约定）；范围按配置原文显示。 */
export function RoutesTable({ routes }: { readonly routes: readonly OrgRoute[] }) {
  if (routes.length === 0) {
    return <p className="org-empty-inline">没有路由约定。</p>;
  }
  return (
    <StackTable
      columns={['种类', '对象', '范围']}
      rows={routes.map((route) => ({ id: route.id, cells: [route.kind, route.target, route.scope] }))}
    />
  );
}
