import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { RailFrame } from '../src/components/RailFrame';

describe('product navigation purity', () => {
  it('renders the six product entries without development module labels', () => {
    render(
      <RailFrame
        active="start"
        footer={null}
        onSelect={() => undefined}
      />,
    );

    const nav = screen.getByRole('navigation', { name: '主导航' });
    const entries = within(nav)
      .getAllByRole('button')
      .map((button) => button.textContent);
    expect(entries).toEqual(['开始', '聊天', 'Agent', '组织', '设备']);
    expect(document.querySelectorAll('[data-dev-label]')).toHaveLength(0);
    expect(document.body).not.toHaveTextContent(/dev-0[1-6]/);
  });

  it('marks the active entry and dispatches selection on click', async () => {
    const user = userEvent.setup();
    const seen: string[] = [];
    render(
      <RailFrame
        active="chat"
        footer={null}
        onSelect={(page) => seen.push(page)}
      />,
    );

    expect(screen.getByRole('button', { name: '聊天' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await user.click(screen.getByRole('button', { name: '开始' }));
    expect(seen).toEqual(['start']);
  });
});
