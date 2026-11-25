/*
 * Decision record (unavailable copy to notifications, PR 1f/F0, 2026-11-14):
 * behaviour contract of the notification port. Operating an unavailable
 * function raises one warning toast `${subject}暂不可用。` in the polite live
 * region that leaves after the usual delay while the center keeps the entry; the technical code is only the entry source in the notification
 * center; a toast must be layered above an open dialog; a missing provider
 * fails loudly.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useReducer, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from '../../src/app/AppShell';
import { Dialog } from '../../src/components/primitives';
import {
  unavailableNotice,
  unavailableText,
  useNotify,
  useNotifyUnavailable,
} from '../../src/components/notifications';
import { Rail } from '../../src/components/Rail';
import { ToastHost } from '../../src/components/ToastHost';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import { appReducer } from '../../src/state/appReducer';
import { DEMO_BASELINE } from '../support/demo-baseline';
import { expectUnavailableToast, renderWithNotifications } from '../support/notify-harness';

function UnavailableButton() {
  const notifyUnavailable = useNotifyUnavailable();
  return (
    <button
      type="button"
      onClick={() => notifyUnavailable('创建 Agent', { code: 'METHOD_NOT_FOUND', source: 'agent.create' })}
    >
      创建
    </button>
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe('unavailableNotice', () => {
  it('formats the text and a source without the reason', () => {
    expect(unavailableText('创建 Agent')).toBe('创建 Agent暂不可用。');
    expect(unavailableNotice('创建 Agent')).toEqual({
      tone: 'warning',
      text: '创建 Agent暂不可用。',
      source: '创建 Agent',
      autoDismiss: true,
    });
    const detailed = unavailableNotice('创建 Agent', {
      code: 'METHOD_NOT_FOUND',
      source: 'agent.create',
      reason: '当前后端目录没有声明 agent.create。',
    });
    expect(detailed.source).toBe('agent.create · METHOD_NOT_FOUND');
    expect(detailed.text).toBe('创建 Agent暂不可用。');
    expect(unavailableNotice('创建 Agent', { code: 'X' }).source).toBe('X');
  });
});

describe('useNotifyUnavailable', () => {
  it('raises one warning toast that leaves after the usual delay', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithNotifications(<UnavailableButton />);
    expect(document.querySelector('.toast-host')).toBeNull();

    await user.click(screen.getByRole('button', { name: '创建' }));
    expectUnavailableToast('创建 Agent暂不可用。');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3900);
    });
    expectUnavailableToast('创建 Agent暂不可用。');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(document.querySelector('.toast')).toBeNull();
  });

  it('keeps a warning without autoDismiss on screen until it is closed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    function Persistent() {
      const notify = useNotify();
      return (
        <button type="button" onClick={() => notify({ tone: 'warning', text: '写入被拒绝。' })}>
          触发
        </button>
      );
    }
    renderWithNotifications(<Persistent />);

    await user.click(screen.getByRole('button', { name: '触发' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });

    expect(document.querySelectorAll('.toast.toast-warning')).toHaveLength(1);
  });

  it('deduplicates repeated operations of the same function', async () => {
    const user = userEvent.setup();
    renderWithNotifications(<UnavailableButton />);

    await user.click(screen.getByRole('button', { name: '创建' }));
    await user.click(screen.getByRole('button', { name: '创建' }));

    expect(document.querySelectorAll('.toast')).toHaveLength(1);
  });

  it('records the entry source in the notification center', async () => {
    const user = userEvent.setup();
    function Shell() {
      const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
      return (
        <>
          <button type="button" onClick={() => dispatch({ type: 'push-notice', ...unavailableNotice('创建 Agent', { code: 'METHOD_NOT_FOUND', source: 'agent.create' }) })}>
            触发
          </button>
          <Rail
            page="start"
            accountMenuOpen={false}
            dispatch={dispatch}
            notifications={{ open: state.notificationCenterOpen, unread: state.centerUnread, items: state.centerNotices }}
          />
        </>
      );
    }
    render(<Shell />);

    await user.click(screen.getByRole('button', { name: '触发' }));
    await user.click(screen.getByRole('button', { name: /通知中心/ }));

    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getByText('创建 Agent暂不可用。')).toBeInTheDocument();
    expect(within(panel).getByText('来源 · agent.create · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('keeps the entry in the notification center after its toast left', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    function Shell() {
      const [state, dispatch] = useReducer(appReducer, undefined, () => createFixtureState(DEMO_BASELINE));
      return (
        <>
          <button type="button" onClick={() => dispatch({ type: 'push-notice', ...unavailableNotice('创建 Agent') })}>
            触发
          </button>
          <ToastHost notices={state.notices} dispatch={dispatch} />
          <Rail
            page="start"
            accountMenuOpen={false}
            dispatch={dispatch}
            notifications={{ open: state.notificationCenterOpen, unread: state.centerUnread, items: state.centerNotices }}
          />
        </>
      );
    }
    render(<Shell />);

    await user.click(screen.getByRole('button', { name: '触发' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4100);
    });
    expect(document.querySelector('.toast')).toBeNull();
    await user.click(screen.getByRole('button', { name: /通知中心/ }));

    const panel = screen.getByRole('dialog', { name: '通知中心' });
    expect(within(panel).getByText('创建 Agent暂不可用。')).toBeInTheDocument();
  });

  it('fails loudly without a provider', async () => {
    const user = userEvent.setup();
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const thrown: unknown[] = [];
    function Probe() {
      const notifyUnavailable = useNotifyUnavailable();
      return (
        <button
          type="button"
          onClick={() => {
            try {
              notifyUnavailable('创建 Agent');
            } catch (error) {
              thrown.push(error);
            }
          }}
        >
          触发
        </button>
      );
    }
    render(<Probe />);

    await user.click(screen.getByRole('button', { name: '触发' }));

    expect((thrown[0] as Error).message).toBe('notifications provider is missing');
    errors.mockRestore();
  });
});

describe('AppShell notifications', () => {
  it('mounts the toast host in the first-run frame', async () => {
    function FirstRun() {
      const [state, dispatch] = useReducer(appReducer, undefined, () => ({
        ...createFixtureState(DEMO_BASELINE),
        page: 'first-run' as const,
      }));
      return (
        <>
          <button type="button" onClick={() => dispatch({ type: 'push-notice', ...unavailableNotice('导出报告') })}>
            触发
          </button>
          <AppShell state={state} dispatch={dispatch} firstRunWizardEnabled />
        </>
      );
    }
    const user = userEvent.setup();
    render(<FirstRun />);
    expect(document.querySelector('.app-first-run')).not.toBeNull();

    await user.click(screen.getByRole('button', { name: '触发' }));

    const host = document.querySelector('.app-first-run .toast-host');
    expect(host).not.toBeNull();
    expect(host).toHaveTextContent('导出报告暂不可用。');
    expect(document.querySelectorAll('.toast-host')).toHaveLength(1);
  });

  it('mounts one toast host and the provider in the signed-out frames of the regular shell', async () => {
    function SignedOut({ page }: { readonly page: 'start' | 'chat' }) {
      const [state, dispatch] = useReducer(appReducer, undefined, () => ({
        ...createFixtureState(DEMO_BASELINE),
        page,
      }));
      return (
        <>
          <button type="button" onClick={() => dispatch({ type: 'push-notice', ...unavailableNotice('导出报告') })}>
            触发
          </button>
          <AppShell state={state} dispatch={dispatch} firstRunWizardEnabled={false} />
        </>
      );
    }
    const user = userEvent.setup();
    for (const page of ['chat', 'start'] as const) {
      const view = render(<SignedOut page={page} />);
      expect(document.querySelector('.app-first-run')).toBeNull();
      if (page === 'chat') {
        expect(screen.getByRole('button', { name: '去登录' })).toBeInTheDocument();
      }

      await user.click(screen.getByRole('button', { name: '触发' }));

      expect(document.querySelectorAll('.toast-host')).toHaveLength(1);
      expect(document.querySelector('.app > .toast-host')).toHaveTextContent('导出报告暂不可用。');
      view.unmount();
    }
  });
});

describe('toast layering', () => {
  it('is above the dialog layers', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8');
    const token = (name: string): number => Number(new RegExp(`--ui-z-${name}:\\s*(\\d+)`).exec(css)?.[1]);
    expect(token('toast')).toBeGreaterThan(token('dialog'));
    expect(token('toast')).toBeGreaterThan(token('dialog-popover'));
  });

  it('keeps the developer tool layer above the toast layer', () => {
    const tokens = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8');
    const dev = readFileSync(resolve(process.cwd(), 'src/dev/dev-module-index.css'), 'utf8');
    const toast = Number(/--ui-z-toast:\s*(\d+)/.exec(tokens)?.[1]);
    const layer = /\.dev-tools-layer\s*\{[^}]*z-index:\s*calc\(var\(--ui-z-toast\)\s*\+\s*(\d+)\)/s.exec(dev);
    expect(layer, 'the dev layer z-index must be derived from the toast token').not.toBeNull();
    expect(toast).toBeGreaterThan(0);
    expect(Number(layer![1])).toBeGreaterThan(0);
  });

  it('shows a toast raised from an open dialog', async () => {
    const user = userEvent.setup();
    function FromDialog() {
      const notifyUnavailable = useNotifyUnavailable();
      const [open, setOpen] = useState(true);
      return (
        <Dialog open={open} title="新建组织" onClose={() => setOpen(false)}>
          <button type="button" onClick={() => notifyUnavailable('新建组织')}>
            提交
          </button>
        </Dialog>
      );
    }
    renderWithNotifications(<FromDialog />);

    await user.click(screen.getByRole('button', { name: '提交' }));

    expect(screen.getByRole('dialog', { name: '新建组织' })).toBeInTheDocument();
    expectUnavailableToast('新建组织暂不可用。');
  });
});
