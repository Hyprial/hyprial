import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { Button, Select } from '../../../components/primitives';
import { ORG_COPY } from '../domain/copy';

export interface TabItem<T extends string> {
  readonly id: T;
  readonly label: string;
  readonly disabled?: boolean;
  readonly reason?: string;
  /** 角标只来自后端数据；没有数据时不传。 */
  readonly badge?: number;
}

/**
 * 子页签与分段切换共用的 tablist：方向键、Home/End 切换，选中即激活。
 * `narrowLabel` 存在时，窄屏改为下拉选择（第 12 节）；下拉的名称只给读屏，选中项本身可读。
 */
export function TabList<T extends string>({
  items,
  value,
  onChange,
  label,
  idPrefix,
  variant,
  narrowLabel,
}: {
  readonly items: readonly TabItem<T>[];
  readonly value: T;
  readonly onChange: (next: T) => void;
  readonly label: string;
  readonly idPrefix: string;
  readonly variant: 'tabs' | 'segments';
  readonly narrowLabel?: string;
}) {
  const refs = useRef(new Map<T, HTMLButtonElement>());
  const enabled = items.filter((item) => !item.disabled);

  function move(event: KeyboardEvent<HTMLDivElement>): void {
    const index = enabled.findIndex((item) => item.id === value);
    let next: TabItem<T> | undefined;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = enabled[(index + 1) % enabled.length];
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      next = enabled[(index - 1 + enabled.length) % enabled.length];
    } else if (event.key === 'Home') {
      next = enabled[0];
    } else if (event.key === 'End') {
      next = enabled[enabled.length - 1];
    }
    if (!next) {
      return;
    }
    event.preventDefault();
    onChange(next.id);
    refs.current.get(next.id)?.focus();
  }

  return (
    <div className={`org-tablist org-tablist--${variant}${narrowLabel ? ' org-tablist--collapsible' : ''}`}>
      <div
        className="org-tablist__list"
        role="tablist"
        aria-label={label}
        onKeyDown={move}
      >
        {items.map((item) => {
          const selected = item.id === value;
          return (
            <button
              key={item.id}
              ref={(node) => {
                if (node) {
                  refs.current.set(item.id, node);
                } else {
                  refs.current.delete(item.id);
                }
              }}
              type="button"
              role="tab"
              id={`${idPrefix}-tab-${item.id}`}
              aria-selected={selected}
              aria-controls={`${idPrefix}-panel`}
              aria-disabled={item.disabled || undefined}
              title={item.disabled ? item.reason : undefined}
              tabIndex={selected ? 0 : -1}
              className="org-tablist__tab"
              data-org-tab={item.id}
              onClick={() => {
                if (!item.disabled) {
                  onChange(item.id);
                }
              }}
            >
              <span>{item.label}</span>
              {item.badge && item.badge > 0 ? (
                <span className="org-tablist__badge">
                  {item.badge}
                  <span className="ui-visually-hidden"> 项待处理</span>
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      {narrowLabel ? (
        // 窄屏的同一组页签；禁用的页签不进入下拉。
        <div className="org-tablist__narrow">
          <label htmlFor={`${idPrefix}-narrow`}>{narrowLabel}</label>
          <Select
            id={`${idPrefix}-narrow`}
            value={value}
            options={items
              .filter((item) => !item.disabled)
              .map((item) => ({
                value: item.id,
                label: item.badge && item.badge > 0 ? `${item.label}（${item.badge}）` : item.label,
              }))}
            onValueChange={(next) => onChange(next as T)}
          />
        </div>
      ) : null}
    </div>
  );
}

/** tablist 对应的面板容器。 */
export function TabPanel({
  idPrefix,
  value,
  children,
}: {
  readonly idPrefix: string;
  readonly value: string;
  readonly children: ReactNode;
}) {
  return (
    <div
      className="org-tabpanel"
      role="tabpanel"
      id={`${idPrefix}-panel`}
      aria-labelledby={`${idPrefix}-tab-${value}`}
    >
      {children}
    </div>
  );
}

/**
 * L2 侧栏：不是模态。打开时焦点进入标题，关闭后回到触发控件。
 * 页头用面包屑回退（与 Agent 页一致）：父级点击即关闭侧栏，当前级为标题；
 * 窄屏改为全屏页，左上角另有“返回”（第 12 节）。Esc 也可关闭。
 */
export function SidePanel({
  title,
  parentLabel,
  onClose,
  children,
  className,
}: {
  readonly title: string;
  readonly parentLabel: string;
  readonly onClose: () => void;
  readonly children: ReactNode;
  readonly className?: string;
}) {
  const titleId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    headingRef.current?.focus({ preventScroll: true });
    return () => {
      if (previous && document.contains(previous)) {
        previous.focus({ preventScroll: true });
      }
    };
  }, []);

  return (
    <aside
      className={`org-side-panel${className ? ` ${className}` : ''}`}
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          onCloseRef.current();
        }
      }}
    >
      <button type="button" className="org-side-panel__back" onClick={() => onClose()}>
        <span aria-hidden="true">‹</span>
        返回{parentLabel}
      </button>
      <nav className="org-breadcrumb" aria-label="面包屑">
        <ol>
          <li>
            <a
              href="#org"
              onClick={(event) => {
                event.preventDefault();
                onClose();
              }}
            >
              {parentLabel}
            </a>
          </li>
          <li aria-current="page">
            <h3 id={titleId} ref={headingRef} tabIndex={-1}>
              {title}
            </h3>
          </li>
        </ol>
      </nav>
      <div className="org-side-panel__body">{children}</div>
    </aside>
  );
}

/** 区块标题与“更新于 hh:mm / 刷新”。 */
export function SectionHeader({
  title,
  description,
  updatedAt,
  onRefresh,
  refreshing,
  actions,
  headingLevel = 2,
}: {
  readonly title: string;
  readonly description?: string;
  readonly updatedAt?: string | null;
  readonly onRefresh?: () => void;
  readonly refreshing?: boolean;
  readonly actions?: ReactNode;
  readonly headingLevel?: 2 | 3;
}) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <div className="org-section-header">
      <div className="org-section-header__text">
        <Heading>{title}</Heading>
        {description ? <p>{description}</p> : null}
      </div>
      <div className="org-section-header__actions">
        {updatedAt ? (
          <span className="org-updated">{ORG_COPY.updatedAt(updatedAt)}</span>
        ) : null}
        {onRefresh ? (
          <Button size="sm" variant="secondary" loading={refreshing} onClick={onRefresh}>
            {ORG_COPY.refresh}
          </Button>
        ) : null}
        {actions}
      </div>
    </div>
  );
}

/** 一次动作的本地忙碌状态，防止重复提交。 */
export function useBusy(): {
  readonly busy: boolean;
  run: <T>(task: () => Promise<T>) => Promise<T | undefined>;
} {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  // StrictMode 开发模式会卸载再挂载一次 effect：重新挂载时必须恢复，否则忙碌状态不再复位。
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function run<T>(task: () => Promise<T>): Promise<T | undefined> {
    if (busyRef.current) {
      return undefined;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      return await task();
    } finally {
      busyRef.current = false;
      if (mounted.current) {
        setBusy(false);
      }
    }
  }
  return { busy, run };
}

/** 列表区块的“状态未确认”占位，附“刷新”。 */
export function UnknownBlock({
  text = ORG_COPY.unknown,
  detail,
  onRefresh,
}: {
  readonly text?: string;
  readonly detail?: string;
  readonly onRefresh?: () => void;
}) {
  return (
    <div className="org-state-block" role="status">
      <p className="org-state-block__title">{text}</p>
      {detail ? <p className="org-state-block__detail">{detail}</p> : null}
      {onRefresh ? (
        <Button size="sm" variant="secondary" onClick={onRefresh}>
          {ORG_COPY.refresh}
        </Button>
      ) : null}
    </div>
  );
}

/** 功能未接通或未立项的说明，不画可操作控件。 */
/** 不在中间断行的短语：时间、“主人 姚”这类。 */
export function Keep({ children }: { readonly children: ReactNode }) {
  return <span className="org-keep">{children}</span>;
}

/** 元信息各段：段内不断行，只在“·”之后换行（窄屏下不出现“主 / 人 姚”“10- / 29”）。 */
export function MetaParts({ parts }: { readonly parts: readonly string[] }) {
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index > 0 ? ' ' : null}
          <Keep>
            {part}
            {index < parts.length - 1 ? ' ·' : null}
          </Keep>
        </Fragment>
      ))}
    </>
  );
}

export function UnavailableBlock({ text }: { readonly text: string }) {
  return (
    <p className="org-unavailable" role="note">
      {text}
    </p>
  );
}

export function nowLabel(): string {
  const date = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 等本次渲染完成（控件解除禁用）后再移动焦点。 */
export function focusSoon(ref: { readonly current: HTMLElement | null }): void {
  window.setTimeout(() => ref.current?.focus(), 0);
}
