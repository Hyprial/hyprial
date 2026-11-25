// B-FIX-008：共享动作区组件契约（single 满宽 / pair 左右）。
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StepActions } from '../src/features/onboarding/impl/StepActions';

describe('StepActions', () => {
  it('renders a single full-width primary action', async () => {
    const onClick = vi.fn();
    render(
      <StepActions
        model={{ kind: 'single', primary: { label: '登录 Hyprial', onClick } }}
      />,
    );
    const row = screen.getByRole('button', { name: '登录 Hyprial' }).closest('.dev01-v17-actions');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('data-actions')).toBe('single');
    const button = screen.getByRole('button', { name: '登录 Hyprial' });
    expect(button.className).toContain('ui-button--primary');
    expect(button.className).toContain('ui-button--full');
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders a pair with the start action on the left and the end action last', async () => {
    const back = vi.fn();
    const next = vi.fn();
    render(
      <StepActions
        model={{
          kind: 'pair',
          start: { label: '返回上一步', onClick: back },
          end: { label: '保存并继续', onClick: next },
        }}
      />,
    );
    const row = screen.getByRole('button', { name: '返回上一步' }).closest('.dev01-v17-actions');
    expect(row?.getAttribute('data-actions')).toBe('pair');
    const buttons = [...row!.querySelectorAll('button')];
    expect(buttons).toHaveLength(2);
    expect(buttons[0].textContent).toBe('返回上一步');
    expect(buttons[1].textContent).toBe('保存并继续');
    expect(buttons[0].className).toContain('dev01-v17-actions__start');
    expect(buttons[1].className).toContain('dev01-v17-actions__end');
    await userEvent.click(buttons[1]);
    expect(next).toHaveBeenCalledTimes(1);
    expect(back).not.toHaveBeenCalled();
  });

  it('auto-disables non-submit actions without a handler', async () => {
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <form onSubmit={submit}>
        <StepActions
          model={{
            kind: 'pair',
            start: { label: '返回上一步' },
            end: { label: '保存并继续', type: 'submit' },
          }}
        />
      </form>,
    );
    expect(screen.getByRole('button', { name: '返回上一步' })).toBeDisabled();
    const end = screen.getByRole('button', { name: '保存并继续' });
    expect(end).toBeEnabled();
    await userEvent.click(end);
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it('keeps an explicit disabled action disabled even with a handler', async () => {
    const onClick = vi.fn();
    render(
      <StepActions
        model={{ kind: 'single', primary: { label: '登录 Hyprial', onClick, disabled: true } }}
      />,
    );
    const button = screen.getByRole('button', { name: '登录 Hyprial' });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('marks a loading action busy and unclickable', async () => {
    const onClick = vi.fn();
    render(
      <StepActions
        model={{ kind: 'single', primary: { label: '正在准备…', onClick, loading: true } }}
      />,
    );
    const button = screen.getByRole('button', { name: '正在准备…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('keeps a disabled end action clickable-safe', async () => {
    const next = vi.fn();
    render(
      <StepActions
        model={{
          kind: 'pair',
          start: { label: '返回上一步' },
          end: { label: '重试准备', onClick: next, disabled: true },
        }}
      />,
    );
    const end = screen.getByRole('button', { name: '重试准备' });
    expect(end).toBeDisabled();
    await userEvent.click(end);
    expect(next).not.toHaveBeenCalled();
  });
});
