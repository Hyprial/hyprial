// ACCOUNT-010：trigger 可见文案统一为「我的」，无可见副标题，行为与可访问名保留。
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AccountMenu } from '../../src/components/AccountMenu';

const base = {
  onOpenChange: vi.fn(),
  onNavigateSettings: vi.fn(),
  onNavigateConnections: vi.fn(),
  onLogout: vi.fn(),
};

describe('account menu trigger copy', () => {
  it('shows exactly 我的 as the visible copy without a subtitle', () => {
    const { container } = render(<AccountMenu open={false} {...base} />);
    const copy = container.querySelector('.account-copy');
    expect(copy?.textContent?.trim()).toBe('我的');
    expect(copy?.querySelector('small')).toBeNull();
    expect(screen.queryByText('我的账号')).toBeNull();
    expect(screen.queryByText('账号与连接')).toBeNull();
  });

  it('keeps the descriptive accessible name and opens the menu', async () => {
    const onOpenChange = vi.fn();
    render(<AccountMenu open={false} {...base} onOpenChange={onOpenChange} />);
    const trigger = screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' });
    expect(trigger).toBeInTheDocument();
    await userEvent.click(trigger);
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });
});
