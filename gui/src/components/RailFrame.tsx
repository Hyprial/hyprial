import { Fragment, type ReactNode } from 'react';
import markOnDark from '../assets/brand/hyprial-mark-dark.svg';
import markOnLight from '../assets/brand/hyprial-mark.svg';
import { Icon, type IconName } from './primitives/Icon';

/**
 * The navigation shape shared by the fixture and live shells. Page ids are
 * presentation labels only; each shell maps them onto its own page state and
 * keeps the canonical capability/error reporting on the page itself.
 */
export type RailPage =
  | 'start'
  | 'chat'
  | 'agents'
  | 'org'
  | 'device';

const NAV_GROUPS: readonly {
  readonly label: string | null;
  readonly items: readonly {
    readonly id: RailPage;
    readonly label: string;
    readonly icon: IconName;
  }[];
}[] = [
  {
    label: null,
    items: [{ id: 'start', label: '开始', icon: 'house' }],
  },
  {
    label: '工作',
    items: [{ id: 'chat', label: '聊天', icon: 'chat-circle' }],
  },
  {
    label: '查找',
    items: [
      { id: 'agents', label: 'Agent', icon: 'robot' },
      { id: 'org', label: '组织', icon: 'tree-structure' },
      { id: 'device', label: '设备', icon: 'desktop' },
    ],
  },
];

interface RailFrameProps {
  readonly active: RailPage | null;
  readonly onSelect: (page: RailPage) => void;
  readonly footer: ReactNode;
  /** Destinations without a source in this composition: no entry is rendered. */
  readonly hiddenPages?: ReadonlySet<RailPage>;
  /** The mobile surface has no organization projection. */
  readonly showOrg?: boolean;
}

function visibleGroups(hiddenPages: ReadonlySet<RailPage> | undefined, showOrg: boolean) {
  return NAV_GROUPS
    .map((group) => ({
      ...group,
      items: group.items.filter(
        (item) => (showOrg || item.id !== 'org') && !hiddenPages?.has(item.id),
      ),
    }))
    .filter((group) => group.items.length > 0);
}

export function RailFrame({
  active,
  onSelect,
  footer,
  hiddenPages,
  showOrg = true,
}: RailFrameProps) {
  return (
    <aside className="rail">
      <div className="brand" role="img" aria-label="Hyprial OS">
        <img className="brand-mark brand-mark--on-rail" src={markOnDark} alt="" />
        <img className="brand-mark brand-mark--on-ground" src={markOnLight} alt="" />
      </div>

      <nav className="nav" aria-label="主导航">
        {visibleGroups(hiddenPages, showOrg).map((group, index) => (
          <Fragment key={group.label ?? 'primary'}>
            {index > 0 ? (
              <div className="nav-separator" aria-hidden="true" />
            ) : null}
            <div
              className="nav-group"
              role={group.label ? 'group' : undefined}
              aria-label={group.label ?? undefined}
            >
              {group.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="nav-item"
                  data-nav={item.id}
                  aria-current={active === item.id ? 'page' : undefined}
                  onClick={() => onSelect(item.id)}
                >
                  <Icon
                    name={item.icon}
                    size="md"
                    active={active === item.id}
                    className="nav-item__icon"
                  />
                  <span className="nav-item-label">{item.label}</span>
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
