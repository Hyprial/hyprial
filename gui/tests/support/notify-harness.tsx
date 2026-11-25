/*
 * Decision record (PR 1f/F0): shared harness for components that report
 * through the notification mechanism. It mounts the same provider and toast
 * host the shared AppShell mounts, over the real reducer, so a test observes
 * what a user would see.
 */
import { render, within, type RenderResult } from '@testing-library/react';
import { useReducer, type ReactNode } from 'react';
import { expect } from 'vitest';
import { ToastHost } from '../../src/components/ToastHost';
import { NotificationsProvider } from '../../src/components/notifications';
import { appReducer } from '../../src/state/appReducer';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import { DEMO_BASELINE } from './demo-baseline';

function Harness({ children }: { readonly children: ReactNode }) {
  const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
  return (
    <NotificationsProvider dispatch={dispatch}>
      {children}
      <ToastHost notices={state.notices} dispatch={dispatch} />
    </NotificationsProvider>
  );
}

export function renderWithNotifications(ui: ReactNode): RenderResult {
  return render(<Harness>{ui}</Harness>);
}

export function expectUnavailableToast(text: string): void {
  const host = document.querySelector('.toast-host');
  expect(host).not.toBeNull();
  expect(host).toHaveAttribute('role', 'status');
  expect(host).toHaveAttribute('aria-live', 'polite');
  const toasts = host!.querySelectorAll('.toast.toast-warning');
  expect(toasts).toHaveLength(1);
  expect(toasts[0]!.querySelector('.toast-text')).toHaveTextContent(text);
  expect(toasts[0]!.querySelector('.toast-text')!.textContent).toBe(text);
  expect(within(toasts[0] as HTMLElement).getByRole('button', { name: '关闭提示' })).toBeInTheDocument();
}
