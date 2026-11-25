// BUTTON-007：jump action 必须是共享 compact Button（真实渲染 + 点击回调）。
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { OverviewTab } from '../../src/features/agents/impl/detail/OverviewTab';

describe('agent jump action', () => {
  it('renders as a compact shared Button and forwards clicks', async () => {
    const onJumpTo = vi.fn();
    render(
      <OverviewTab
        notices={[{ id: 'n1', tone: 'warning', text: '需要处理', jumpTo: 'credentials' } as never]}
        onJumpTo={onJumpTo}
      />,
    );
    const jump = document.querySelector('.agent-jump-link-layout') as HTMLElement;
    expect(jump).not.toBeNull();
    expect(jump.getAttribute('data-slot')).toBe('button');
    expect(jump.className).toContain('ui-button--compact');
    expect(jump.className).toContain('ui-button--text');
    await userEvent.click(jump);
    expect(onJumpTo).toHaveBeenCalledWith('credentials');
  });
});
