import {
  useEffect,
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { AccountIcon } from './Icon';

interface AccountMenuProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly onNavigateSettings: () => void;
  readonly onNavigateConnections: () => void;
  readonly onHelp: () => void;
  readonly onLogout: () => void;
}

export function AccountMenu({
  open,
  onOpenChange,
  onNavigateSettings,
  onNavigateConnections,
  onHelp,
  onLogout,
}: AccountMenuProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !rootRef.current?.contains(target)) {
        onOpenChange(false);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open, onOpenChange]);

  useEffect(() => {
    if (open) {
      menuRef.current
        ?.querySelector<HTMLElement>('[role="menuitem"]')
        ?.focus();
    }
  }, [open]);

  const close = () => onOpenChange(false);

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    if (items.length === 0) {
      return;
    }
    const currentIndex = items.indexOf(document.activeElement as HTMLElement);

    switch (event.key) {
      case 'ArrowDown': {
        event.preventDefault();
        const next = items[(currentIndex + 1) % items.length];
        next.focus();
        break;
      }
      case 'ArrowUp': {
        event.preventDefault();
        const previous =
          items[(currentIndex - 1 + items.length) % items.length];
        previous.focus();
        break;
      }
      case 'Home':
        event.preventDefault();
        items[0].focus();
        break;
      case 'End':
        event.preventDefault();
        items[items.length - 1].focus();
        break;
      case 'Escape':
        event.preventDefault();
        close();
        triggerRef.current?.focus();
        break;
      case 'Tab':
        close();
        break;
      default:
        break;
    }
  };

  const handleConnections = () => {
    close();
    onNavigateConnections();
  };

  const handleSettings = () => {
    close();
    onNavigateSettings();
  };

  const handleHelp = () => {
    close();
    onHelp();
  };

  const handleLogout = () => {
    close();
    onLogout();
  };

  return (
    <div className="account" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="account-trigger"
        aria-label="我的账号与连接菜单"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? 'account-menu' : undefined}
        onClick={() => onOpenChange(!open)}
      >
        <span className="account-avatar" aria-hidden="true" />
        <span className="account-copy">
          <strong>我的账号</strong>
          <small>账号与连接</small>
        </span>
        <span className="account-chevron" aria-hidden="true">
          {open ? '▴' : '▾'}
        </span>
      </button>

      {open && (
        <div
          id="account-menu"
          ref={menuRef}
          className="account-menu"
          role="menu"
          aria-label="账号菜单"
          onKeyDown={handleMenuKeyDown}
        >
          <button
            type="button"
            role="menuitem"
            onClick={handleSettings}
          >
            <AccountIcon name="settings" />
            <span>设置</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={handleConnections}
          >
            <AccountIcon name="connection" />
            <span>连接</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={handleHelp}
          >
            <AccountIcon name="help" />
            <span>帮助</span>
          </button>
          <div className="menu-separator" role="separator" />
          <button
            type="button"
            role="menuitem"
            onClick={handleLogout}
          >
            <AccountIcon name="logout" />
            <span>登出</span>
          </button>
        </div>
      )}
    </div>
  );
}
