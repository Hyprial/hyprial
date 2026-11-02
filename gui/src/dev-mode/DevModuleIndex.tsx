import { DEV_MODULES } from './module-registry';

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

export function DevModuleIndex({
  enabled = __HYPRIAL_DEV_MODULE_LABELS__,
}: {
  readonly enabled?: boolean;
}) {
  if (!enabled) {
    return null;
  }

  return (
    <div className="dev-module-dock">
      <span className="dev-module-scroll-hint">横向滚动查看全部模块</span>
      <aside
        aria-label="开发模块索引"
        className="dev-module-index"
        role="list"
        tabIndex={-1}
      >
        {DEV_MODULES.map((module) => (
          <span
            aria-label={`${module.id} ${module.name}：暂不可用`}
            className="dev-module-entry"
            data-dev-module={module.id}
            data-testid={`dev-module-${module.id}`}
            key={module.id}
            role="listitem"
            title={module.scope.join('、')}
          >
            <strong>{module.id}</strong>
            <span>{module.name}</span>
            <small>暂不可用</small>
          </span>
        ))}
      </aside>
    </div>
  );
}
