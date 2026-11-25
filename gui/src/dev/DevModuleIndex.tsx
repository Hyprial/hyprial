import { DEV_MODULES } from './module-registry';

export function DevModuleIndex({
  enabled = true,
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
            aria-label={`${module.id} ${module.name}`}
            className="dev-module-entry"
            data-dev-module={module.id}
            data-testid={`dev-module-${module.id}`}
            key={module.id}
            role="listitem"
            title={module.scope.join('、')}
          >
            <strong>{module.id}</strong>
            <span>{module.name}</span>
          </span>
        ))}
      </aside>
    </div>
  );
}
