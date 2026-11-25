import { StrictMode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { FirstRunOnboarding, type FirstRunActions, type SetupSnapshot } from '../src/features/onboarding';
import { stubFirstRunActions } from './support/first-run-actions';

const saved: SetupSnapshot = { keySaved: true, pendingAction: null };
function actions(overrides: Partial<FirstRunActions> = {}): FirstRunActions {
  return { ...stubFirstRunActions(),
    getAccountState: async () => ({ account: { status: 'authenticated', owner: 'test@example.invalid' }, device: { status: 'ready' } }),
    getSetupState: async () => saved,
    saveModelKey: async () => ({ status: 'saved' }),
    ...overrides,
  };
}
async function model(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  await screen.findByRole('heading', { name: '模型服务' });
}

describe('durable first-run state restoration', () => {
  it('restores a saved key and advances directly from model to conversation without saving again', async () => {
    const saveModelKey = vi.fn();
    const user = userEvent.setup();
    render(<StrictMode><FirstRunOnboarding actions={actions({ saveModelKey })} /></StrictMode>);
    await model(user);
    expect(await screen.findByText('已保存 DeepSeek Key')).toBeInTheDocument();
    expect(screen.queryByLabelText(/DeepSeek API Key/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByRole('heading', { name: '首次对话' })).toBeInTheDocument();
    expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'conversation');
    expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();
    expect(saveModelKey).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toMatch(/聊天能力尚未接入|聊天服务未连接/);
  });

  it('restores saved key metadata after login when the initial account read fails', async () => {
    const getAccountState = vi.fn()
      .mockRejectedValueOnce(new Error('status read failed'))
      .mockResolvedValue({
        account: { status: 'authenticated', owner: 'after-login@example.invalid' },
        device: { status: 'ready' },
      });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({
      getAccountState,
      authenticateAccount: async () => ({
        status: 'authenticated',
        owner: 'after-login@example.invalid',
      }),
    })} />);
    expect(await screen.findByText('登录结果未知')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    await model(user);
    expect(await screen.findByText('已保存 DeepSeek Key')).toBeInTheDocument();
    expect(screen.queryByLabelText(/DeepSeek API Key/)).not.toBeInTheDocument();
  });

  it('restores saved key metadata after retry cancellation confirms an already authenticated account', async () => {
    vi.useFakeTimers();
    try {
      let reads = 0;
      const getAccountState = vi.fn(async () => {
        reads += 1;
        if (reads === 1) throw new Error('status read failed');
        return {
          account: { status: 'authenticated' as const, owner: 'after-cancel@example.invalid' },
          device: { status: 'ready' as const },
        };
      });
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
      const saveModelKey = vi.fn();
      const getSetupState = vi.fn(async () => saved);
      render(<FirstRunOnboarding actions={actions({
        getAccountState, authenticateAccount, cancelAccount, saveModelKey,
        getSetupState,
      })} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText('登录结果未知')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: '登录 Hyprial' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(90 * 1000); });
      fireEvent.click(screen.getByRole('button', { name: '重试登录' }));
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(cancelAccount).toHaveBeenCalledTimes(1);
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole('button', { name: '继续到设备' }));
      fireEvent.click(screen.getByRole('button', { name: '继续到运行配置' }));
      expect(screen.getByRole('heading', { name: '模型服务' })).toBeInTheDocument();
      expect(screen.getByText('已保存 DeepSeek Key')).toBeInTheDocument();
      expect(screen.queryByLabelText(/DeepSeek API Key/)).not.toBeInTheDocument();
      expect(saveModelKey).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not restore a saved gate from unknown key metadata', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({ getSetupState: async () => ({ ...saved, keySaved: null }) })} />);
    await model(user);
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
  });

  it('does not overwrite an edited key gate with a late restore response', async () => {
    let finish!: (snapshot: SetupSnapshot) => void;
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({ getSetupState: () => new Promise(resolve => { finish = resolve; }) })} />);
    await model(user);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'new-unconfirmed-value');
    await act(async () => finish(saved));
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
  });

  it('does not restore a saved gate while the backend still reports a pending save', async () => {
    const saveModelKey = vi.fn();
    const getSetupState = vi.fn(async () => ({ ...saved, pendingAction: 'save' as const }));
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({ getSetupState, saveModelKey })} />);
    await model(user);
    expect(screen.getByLabelText(/DeepSeek API Key/)).toBeInTheDocument();
    expect(await screen.findByText('此前保存结果未知')).toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(saveModelKey).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '检查状态' }));
    expect(await screen.findByText(/保存仍未完成/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  });

  it('keeps the key gate closed after login when the backend reports a pending save', async () => {
    const getAccountState = vi.fn()
      .mockRejectedValueOnce(new Error('status read failed'))
      .mockResolvedValue({
        account: { status: 'authenticated', owner: 'after-login@example.invalid' },
        device: { status: 'ready' },
      });
    const getSetupState = vi.fn(async () => ({ ...saved, pendingAction: 'save' as const }));
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({
      getAccountState,
      getSetupState,
      authenticateAccount: async () => ({
        status: 'authenticated',
        owner: 'after-login@example.invalid',
      }),
    })} />);
    expect(await screen.findByText('登录结果未知')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    await model(user);
    expect(await screen.findByText('此前保存结果未知')).toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  });

  it('keeps the key gate closed after confirmed retry cancellation when a save is pending', async () => {
    vi.useFakeTimers();
    try {
      let reads = 0;
      const getAccountState = vi.fn(async () => {
        reads += 1;
        if (reads === 1) throw new Error('status read failed');
        return {
          account: { status: 'authenticated' as const, owner: 'after-cancel@example.invalid' },
          device: { status: 'ready' as const },
        };
      });
      const authenticateAccount = vi.fn(() => new Promise<never>(() => {}));
      const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
      const getSetupState = vi.fn(async () => ({ ...saved, pendingAction: 'save' as const }));
      render(<FirstRunOnboarding actions={actions({
        getAccountState, authenticateAccount, cancelAccount, getSetupState,
      })} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText('登录结果未知')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: '登录 Hyprial' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      await act(async () => { await vi.advanceTimersByTimeAsync(90 * 1000); });
      fireEvent.click(screen.getByRole('button', { name: '重试登录' }));
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(cancelAccount).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole('button', { name: '继续到设备' }));
      fireEvent.click(screen.getByRole('button', { name: '继续到运行配置' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByRole('heading', { name: '模型服务' })).toBeInTheDocument();
      expect(screen.getByText('此前保存结果未知')).toBeInTheDocument();
      expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps an unconfirmed save closed across step navigation and a read with no pending action', async () => {
    const getSetupState = vi.fn(async () => ({ ...saved, keySaved: false }));
    const saveModelKey = vi.fn(async () => ({
      status: 'unknown' as const,
      message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。',
    }));
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions({ getSetupState, saveModelKey })} />);
    await model(user);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'candidate-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回上一步' }));
    await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
    await screen.findByRole('heading', { name: '模型服务' });
    expect(await screen.findByText('此前保存结果未知')).toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(saveModelKey).toHaveBeenCalledTimes(1);
  });

  it('drops a previous identity saved gate and late snapshot when actions switch', async () => {
    let finishOld!: (snapshot: SetupSnapshot) => void;
    const oldActions = actions({
      getAccountState: async () => ({
        account: { status: 'authenticated', owner: 'old@example.invalid' },
        device: { status: 'ready' },
      }),
      getSetupState: () => new Promise(resolve => { finishOld = resolve; }),
    });
    const newActions = actions({
      getAccountState: async () => ({
        account: { status: 'authenticated', owner: 'new@example.invalid' },
        device: { status: 'ready' },
      }),
      getSetupState: async () => ({ ...saved, pendingAction: 'save' as const }),
    });
    const user = userEvent.setup();
    const view = render(<FirstRunOnboarding actions={oldActions} />);
    await model(user);
    expect(screen.getByLabelText(/DeepSeek API Key/)).toBeInTheDocument();
    view.rerender(<FirstRunOnboarding actions={newActions} />);
    expect(await screen.findByText('当前账号：new@example.invalid')).toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续到设备' }));
    await user.click(screen.getByRole('button', { name: '继续到运行配置' }));
    await screen.findByRole('heading', { name: '模型服务' });
    expect(await screen.findByText('此前保存结果未知')).toBeInTheDocument();
    await act(async () => finishOld(saved));
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.getByText('此前保存结果未知')).toBeInTheDocument();
  });

  it('reports a pending save as unconfirmed instead of prompting another save', async () => {
    const user = userEvent.setup();
    const getSetupState = vi.fn()
      .mockResolvedValueOnce({ ...saved, keySaved: false })
      .mockResolvedValueOnce({ ...saved, keySaved: false, pendingAction: 'save' });
    const saveModelKey = vi.fn(async () => ({
      status: 'unknown' as const,
      message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。',
    }));
    render(<FirstRunOnboarding actions={actions({ getSetupState, saveModelKey })} />);
    await model(user);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'candidate-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '检查状态' }));
    expect(await screen.findByText(/保存仍未完成/)).toBeInTheDocument();
    expect(screen.queryByText(/重新输入并保存/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(getSetupState).toHaveBeenCalledTimes(2);
    expect(saveModelKey).toHaveBeenCalledTimes(1);
  });

  it('uses a read-only setup read after an unknown save without opening the saved gate', async () => {
    const user = userEvent.setup();
    const getSetupState = vi.fn()
      .mockResolvedValueOnce({ ...saved, keySaved: false })
      .mockResolvedValueOnce({ ...saved, keySaved: true });
    const saveModelKey = vi.fn(async () => ({
      status: 'unknown' as const,
      message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。',
    }));
    render(<FirstRunOnboarding actions={actions({ getSetupState, saveModelKey })} />);
    await model(user);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'candidate-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '检查状态' }));
    expect(await screen.findByText(/无法确认是否为本次输入/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(getSetupState).toHaveBeenCalledTimes(2);
    expect(saveModelKey).toHaveBeenCalledTimes(1);
  });
});
