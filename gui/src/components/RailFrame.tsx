import { Fragment, type ReactNode } from 'react';
import type { DevModuleId } from '../dev-mode';

/**
 * The navigation shape shared by the fixture and live shells. Page ids are
 * presentation labels only; each shell maps them onto its own page state and
 * keeps the canonical capability/error reporting on the page itself.
 */
export type RailPage =
  | 'start'
  | 'chat'
  | 'tasks'
  | 'agents'
  | 'org';

const NAV_GROUPS: readonly {
  readonly label: string | null;
  readonly items: readonly { readonly id: RailPage; readonly label: string }[];
}[] = [
  {
    label: null,
    items: [{ id: 'start', label: '开始' }],
  },
  {
    label: '工作',
    items: [
      { id: 'chat', label: '聊天' },
      { id: 'tasks', label: '任务' },
    ],
  },
  {
    label: '查找',
    items: [
      { id: 'agents', label: 'Agent' },
      { id: 'org', label: '组织' },
    ],
  },
];

interface RailFrameProps {
  readonly active: RailPage | null;
  readonly devLabels?: Readonly<Partial<Record<RailPage, DevModuleId>>>;
  readonly onSelect: (page: RailPage) => void;
  readonly footer: ReactNode;
  /** The mobile surface has no organization projection. */
  readonly showOrg?: boolean;
}

export function RailFrame({
  active,
  devLabels,
  onSelect,
  footer,
  showOrg = true,
}: RailFrameProps) {
  return (
    <aside className="rail">
      <div className="brand" aria-label="Hyprial OS">
        <span className="brand-wordmark">Hyprial OS</span>
      </div>

      <nav className="nav" aria-label="主导航">
        {NAV_GROUPS.map((group, index) => (
          <Fragment key={group.label ?? 'primary'}>
            {index > 0 ? (
              <div className="nav-separator" aria-hidden="true" />
            ) : null}
            <div
              className="nav-group"
              role={group.label ? 'group' : undefined}
              aria-label={group.label ?? undefined}
            >
              {group.items
                .filter((item) => showOrg || item.id !== 'org')
                .map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="nav-item"
                    data-nav={item.id}
                    aria-current={active === item.id ? 'page' : undefined}
                    onClick={() => onSelect(item.id)}
                  >
                    <span className="nav-item-label">{item.label}</span>
                    {devLabels?.[item.id] ? (
                      <span
                        aria-hidden="true"
                        className="dev-nav-label"
                        data-dev-label={devLabels[item.id]}
                      >
                        {devLabels[item.id]}
                      </span>
                    ) : null}
                  </button>
                ))}
            </div>
          </Fragment>
        ))}
      </nav>

      <div className="rail-spacer" />
      {footer}
    </aside>
  );
}
