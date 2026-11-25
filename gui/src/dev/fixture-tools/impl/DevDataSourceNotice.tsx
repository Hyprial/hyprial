/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): the local-test-data
 * notice describes the fixture composition only, so it is a fixture tool
 * contribution and no longer lives next to the module index inside the
 * floating tool layer graph. Markup and copy are unchanged.
 */
export function DevDataSourceNotice() {
  return (
    <div
      aria-label="开发预览数据来源"
      className="dev-data-source"
      role="note"
    >
      开发预览：使用本地测试数据，不代表真实账号、Agent、消息或组织状态。
    </div>
  );
}
