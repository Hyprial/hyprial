import { Button } from '../components/primitives';
import { useEffect, useRef, type Dispatch } from 'react';
import type { AppAction } from '../state/types';

export function LogoutDialog({
  dispatch,
}: {
  readonly dispatch: Dispatch<AppAction>;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    cancelRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        dispatch({ type: 'cancel-logout' });
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }

      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) {
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      previous?.focus();
    };
  }, [dispatch]);

  return (
    <div
      className="ui-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          dispatch({ type: 'cancel-logout' });
        }
      }}
    >
      <div
        ref={dialogRef}
        className="ui-dialog logout-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="logout-title"
        aria-describedby="logout-description"
      >
        <h2 id="logout-title" className="ui-dialog__title">登出此账号？</h2>
        <p id="logout-description" className="ui-dialog__description">
          当前只清空本页面会话。
        </p>
        <div className="ui-dialog__actions">
          <Button variant="secondary"
            type="button"
            ref={cancelRef}
            onClick={() => dispatch({ type: 'cancel-logout' })}
          >
            取消
          </Button>
          <Button variant="primary"
            type="button"
            onClick={() => dispatch({ type: 'confirm-logout' })}
          >
            确认登出
          </Button>
        </div>
      </div>
    </div>
  );
}
