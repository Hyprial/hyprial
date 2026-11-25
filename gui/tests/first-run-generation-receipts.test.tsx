import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { FirstRunOnboarding, type FirstRunActions, type ModelKeySaveResult } from '../src/features/onboarding';
import { stubFirstRunActions } from './support/first-run-actions';

function actions(overrides: Partial<FirstRunActions> = {}): FirstRunActions {
  return {
    ...stubFirstRunActions(),
    getAccountState: async () => ({ account: { status: 'authenticated' }, device: { status: 'ready' } }),
    ...overrides,
  };
}

it.each(['saved', 'unknown'] as const)('ignores a retired instance model-save %s receipt in the shared first-run page', async status => {
  let finish!: (result: ModelKeySaveResult) => void;
  const oldSave = vi.fn(() => new Promise<ModelKeySaveResult>(resolve => { finish = resolve; }));
  const oldActions = actions({ saveModelKey: oldSave });
  const user = userEvent.setup();
  const view = render(<FirstRunOnboarding actions={oldActions} />);
  const next = await screen.findByRole('button', { name: '继续到设备' });
  await waitFor(() => expect(next).toBeEnabled());
  await user.click(next);
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  await user.type(screen.getByLabelText(/DeepSeek API Key/), 'synthetic-old-instance');
  await user.click(screen.getByRole('button', { name: '保存并继续' }));
  expect(oldSave).toHaveBeenCalledExactlyOnceWith('synthetic-old-instance');

  const newSave = vi.fn(async () => ({ status: 'saved' as const }));
  view.rerender(<FirstRunOnboarding actions={actions({ saveModelKey: newSave })} />);
  await screen.findByRole('heading', { name: '登录' });
  await act(async () => { finish({ status }); });
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(newSave).not.toHaveBeenCalled();

  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('');
  expect(screen.queryByText('此前保存结果未知')).not.toBeInTheDocument();
  await user.type(screen.getByLabelText(/DeepSeek API Key/), 'synthetic-new-instance');
  await user.click(screen.getByRole('button', { name: '保存并继续' }));
  await screen.findByRole('heading', { name: '首次对话' });
  expect(newSave).toHaveBeenCalledExactlyOnceWith('synthetic-new-instance');
  expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();
});
