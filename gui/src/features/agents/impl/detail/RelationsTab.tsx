/**
 * implG4 (2026-11-02)：所属组织页签。
 *
 * - 组织分区复用 dev-04 AgentOrgPanel；页签只在详情拿到 orgSections 时存在，
 *   不再有预留表格卡与禁用页签（2026-11-14）。
 * - link 与 workflow 读取不在本页签：link 在概览，workflow 没有页面。
 */

import { useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

/**
 * 组织卡（G4-1）：orgSections 未提供数据源时可能渲染为空（如组织线
 * provider 未接入时 AgentOrgPanel 返回 null），卡片不得留白——检测到
 * 空体时显示显式状态行。
 */
function OrgCard({ orgSections }: { readonly orgSections: ReactNode }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [empty, setEmpty] = useState(true);
  useLayoutEffect(() => {
    setEmpty((bodyRef.current?.childElementCount ?? 0) === 0);
  }, [orgSections]);
  return (
    <section
      className="agent-detail-card"
      aria-label="组织"
      data-testid="relations-org"
      data-org-state={empty ? 'unknown' : 'ready'}
    >
      <h3 className="agent-detail-card-title">组织</h3>
      <div ref={bodyRef}>{orgSections}</div>
      {empty && (
        <p className="agent-manage-note" role="status">
          组织状态未确认。
        </p>
      )}
    </section>
  );
}

export function RelationsTab({ orgSections }: { readonly orgSections: ReactNode }) {
  return (
    <div className="agent-detail-grid-two">
      <OrgCard orgSections={orgSections} />
    </div>
  );
}
