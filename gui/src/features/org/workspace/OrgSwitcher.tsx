import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ORG_COPY, ORG_KIND_LABELS, ORG_LIFECYCLE_LABELS } from '../domain/copy';
import { findOrg, orgDisplayName, pendingInvites, sortOrgs } from '../domain/selectors';
import type { OrgDomainSnapshot } from '../domain/types';

/**
 * 组织切换器（F01，4.1）。目标位置是左栏；本版不改壳层，先放在组织 tab 页头。
 * 菜单是 L0 菜单，不是模态：方向键、回车、Esc 可用。
 */
export function OrgSwitcher({
  snapshot,
  switching,
  onSwitch,
  onCreate,
  onJoin,
  onPendingInvites,
}: {
  readonly snapshot: OrgDomainSnapshot;
  readonly switching: boolean;
  readonly onSwitch: (orgId: string) => void;
  readonly onCreate: () => void;
  readonly onJoin: () => void;
  readonly onPendingInvites: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const current = findOrg(snapshot, snapshot.currentOrgId);
  const orgs = snapshot.orgs ? sortOrgs(snapshot.orgs) : [];
  const inviteCount = pendingInvites(snapshot)?.length ?? null;

  let label: string;
  if (switching) {
    label = ORG_COPY.switching;
  } else if (snapshot.orgs === null) {
    label = ORG_COPY.orgUnknown;
  } else if (!current) {
    label = ORG_COPY.noCurrentOrg;
  } else {
    label = orgDisplayName(current);
  }

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const first = menuRef.current?.querySelector<HTMLElement>('[role^="menuitem"]');
    first?.focus();
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointer);
    return () => document.removeEventListener('mousedown', onPointer);
  }, [open]);

  function close(): void {
    setOpen(false);
    buttonRef.current?.focus();
  }

  function onMenuKey(event: KeyboardEvent<HTMLDivElement>): void {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [],
    );
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      items[(index + 1) % items.length]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length]?.focus();
    } else if (event.key === 'Home') {
      event.preventDefault();
      items[0]?.focus();
    } else if (event.key === 'End') {
      event.preventDefault();
      items[items.length - 1]?.focus();
    } else if (event.key === 'Tab') {
      setOpen(false);
    }
  }

  function choose(action: () => void): void {
    setOpen(false);
    action();
  }

  return (
    <div className="org-switcher" data-dev-ui="DEV-UI-ORG-001">
      <button
        ref={buttonRef}
        type="button"
        className="org-switcher__button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`${ORG_COPY.switcherLabel}：${label}`}
        disabled={switching}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' && !open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span className="org-switcher__caption">{ORG_COPY.currentOrg}</span>
        <span className="org-switcher__name">{label}</span>
        {current && !switching ? (
          <span className="org-tag">{ORG_KIND_LABELS[current.kind]}</span>
        ) : null}
        <span aria-hidden="true" className="org-switcher__chevron">
          ▾
        </span>
      </button>
      {open ? (
        <div
          ref={menuRef}
          id={menuId}
          className="org-switcher__menu"
          role="menu"
          aria-label={ORG_COPY.switcherLabel}
          onKeyDown={onMenuKey}
        >
          {orgs.map((org) => {
            const checked = org.id === snapshot.currentOrgId;
            return (
              <button
                key={org.id}
                type="button"
                role="menuitemradio"
                aria-checked={checked}
                className="org-switcher__item"
                tabIndex={-1}
                onClick={() => choose(() => onSwitch(org.id))}
              >
                <span aria-hidden="true" className="org-switcher__check">
                  {checked ? '✓' : ''}
                </span>
                <span className="org-switcher__item-name">{orgDisplayName(org)}</span>
                <span className="org-tag">{ORG_KIND_LABELS[org.kind]}</span>
                {org.lifecycle !== 'active' ? (
                  <span className="org-tag org-tag--warning">
                    {ORG_LIFECYCLE_LABELS[org.lifecycle]}
                  </span>
                ) : null}
              </button>
            );
          })}
          {orgs.length > 0 ? <div role="separator" className="org-switcher__sep" /> : null}
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="org-switcher__item"
            onClick={() => choose(onCreate)}
          >
            {ORG_COPY.createOrg}
          </button>
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="org-switcher__item"
            onClick={() => choose(onJoin)}
          >
            {ORG_COPY.joinOrg}
          </button>
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            className="org-switcher__item"
            onClick={() => choose(onPendingInvites)}
          >
            {ORG_COPY.pendingInvites(inviteCount)}
          </button>
        </div>
      ) : null}
    </div>
  );
}
