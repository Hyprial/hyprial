import {
  useEffect,
  useId,
  useRef,
  type ReactNode,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { Button, type ButtonVariant } from './Button';
import { cx } from './types';

export interface DialogAction {
  readonly label: string;
  readonly onClick: () => void;
  readonly variant?: ButtonVariant;
  readonly disabled?: boolean;
  readonly loading?: boolean;
}

export interface DialogProps {
  readonly open: boolean;
  readonly title: string;
  readonly description?: string;
  readonly children?: ReactNode;
  readonly actions?: ReadonlyArray<DialogAction>;
  readonly onClose: () => void;
  readonly closeOnEscape?: boolean;
  readonly closeOnBackdrop?: boolean;
  /** Optional element to receive initial focus; otherwise the first control does. */
  readonly initialFocusRef?: RefObject<HTMLElement | null>;
  readonly closeLabel?: string;
  readonly className?: string;
}

function getFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(
    (element) =>
      !element.hasAttribute('disabled') &&
      element.getAttribute('aria-hidden') !== 'true',
  );
}

/**
 * Modal presentation shell. Focus is trapped while open, Escape and backdrop
 * closing are explicit props, and focus returns to the trigger on close. The
 * dialog never performs the confirmed action itself; callers own that.
 */
export function Dialog({
  open,
  title,
  description,
  children,
  actions = [],
  onClose,
  closeOnEscape = true,
  closeOnBackdrop = false,
  initialFocusRef,
  closeLabel = '关闭对话框',
  className,
}: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open || typeof document === 'undefined') return undefined;

    const panel = panelRef.current;
    const previous =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const initialTarget =
      initialFocusRef?.current ??
      (panel ? getFocusable(panel)[0] : null) ??
      panel;
    initialTarget?.focus({ preventScroll: true });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && closeOnEscape) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }

      if (event.key !== 'Tab' || !panel) return;

      const focusable = getFocusable(panel);
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus({ preventScroll: true });
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const outsidePanel = !(active instanceof Node) || !panel.contains(active);

      if (event.shiftKey) {
        if (active === first || active === panel || outsidePanel) {
          event.preventDefault();
          last.focus({ preventScroll: true });
        }
      } else if (active === last || active === panel || outsidePanel) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (previous && document.contains(previous)) {
        previous.focus({ preventScroll: true });
      }
    };
  }, [open, closeOnEscape, initialFocusRef]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="ui-dialog-backdrop"
      onMouseDown={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) {
          onCloseRef.current();
        }
      }}
    >
      <div
        ref={panelRef}
        className={cx('ui-dialog', className)}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
      >
        <div className="ui-dialog__header">
          <h2 className="ui-dialog__title" id={titleId}>
            {title}
          </h2>
          <button
            type="button"
            className="ui-dialog__close"
            aria-label={closeLabel}
            onClick={onClose}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
        {description && (
          <p className="ui-dialog__description" id={descriptionId}>
            {description}
          </p>
        )}
        {children && <div className="ui-dialog__body">{children}</div>}
        {actions.length > 0 && (
          <div className="ui-dialog__actions">
            {actions.map((action) => (
              <Button
                key={action.label}
                variant={action.variant ?? 'secondary'}
                disabled={action.disabled}
                loading={action.loading}
                onClick={action.onClick}
              >
                {action.label}
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
