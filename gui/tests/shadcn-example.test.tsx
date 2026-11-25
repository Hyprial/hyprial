import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../src/dev/FixtureApp';
import { createLocalAgentGateway } from '../src/features/agents';
import { Button, Card } from '../src/components/primitives';

describe('shadcn through the project primitives', () => {
  it.each([
    ['primary', 'default'],
    ['secondary', 'outline'],
    ['ghost', 'ghost'],
    ['text', 'link'],
    ['danger', 'destructive'],
  ] as const)('adapts %s without changing button semantics', async (variant, upstream) => {
    const onClick = vi.fn();
    render(<Button variant={variant} size="sm" onClick={onClick}>继续</Button>);
    const button = screen.getByRole('button', { name: '继续' });
    expect(button).toHaveAttribute('data-slot', 'button');
    expect(button).toHaveAttribute('data-variant', upstream);
    expect(button).toHaveAttribute('data-size', 'sm');
    expect(button).toHaveAttribute('type', 'button');
    await userEvent.tab();
    expect(button).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('blocks repeated submissions while loading and allows retry after settlement', async () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button loading onClick={onClick}>创建 Agent</Button>);
    const button = screen.getByRole('button', { name: /创建 Agent/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    await userEvent.click(button);
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();

    rerender(<Button onClick={onClick}>创建 Agent</Button>);
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute('aria-busy');
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('preserves explicit submit, disabled, icon and full-width props', async () => {
    const onSubmit = vi.fn((event) => event.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit" fullWidth leadingIcon={<svg aria-label="decorative" />}>保存</Button>
        <Button disabled type="submit">禁用</Button>
      </form>,
    );
    const save = screen.getByRole('button', { name: '保存' });
    expect(save).toHaveClass('ui-button--full');
    expect(save.querySelector('svg')?.parentElement).toHaveAttribute('aria-hidden', 'true');
    await userEvent.click(save);
    await userEvent.click(screen.getByRole('button', { name: '禁用' }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it.each(['div', 'section', 'article', 'aside'] as const)('keeps the %s card element without an extra wrapper', (as) => {
    const { container } = render(
      <Card as={as} tone="warning" padding="lg" selected interactive aria-label="状态">
        <p>等待处理</p>
      </Card>,
    );
    const card = container.firstElementChild!;
    expect(card.tagName.toLowerCase()).toBe(as);
    expect(card).toHaveAttribute('data-slot', 'card');
    expect(card).toHaveAttribute('data-selected', 'true');
    expect(card).toHaveClass('ui-card--warning', 'ui-card--lg', 'ui-card--interactive');
    expect(card.children).toHaveLength(1);
    expect(card.firstElementChild?.tagName).toBe('P');
  });

  it('replaces the normal shadow when a card is selected', () => {
    const { container } = render(<Card tone="info" selected>已选择</Card>);
    const shadows = container.firstElementChild!.className.split(/\s+/).filter((name) => name.startsWith('tw:shadow-'));
    expect(shadows).toEqual(['tw:shadow-[inset_0_0_0_2px_var(--ui-ink-1)]']);
  });

  it('keeps the project line height when selecting the small font size', () => {
    render(<Button size="sm">小按钮</Button>);
    expect(screen.getByRole('button', { name: '小按钮' })).toHaveClass('tw:leading-[var(--ui-leading-tight)]');
  });
});

describe('start-page shadcn example', () => {
  it('uses Card/Button while preserving the local agent creation journey', async () => {
    const call = vi.fn();
    render(
      <FixtureApp
        dataMode="fixture"
        agentSeed={{ agents: [], connections: [], organizations: [] }}
        agentGateway={createLocalAgentGateway({ agents: [], connections: [], organizations: [] })}
      />,
    );

    const card = screen.getByRole('region', { name: '给下一件事，找一位帮手' });
    expect(card).toHaveAttribute('data-slot', 'card');
    const button = within(card).getByRole('button', { name: '创建 Agent' });
    expect(button).toHaveAttribute('data-slot', 'button');
    await userEvent.click(button);
    expect(screen.getByRole('heading', { level: 2, name: '创建 Agent' })).toBeVisible();
    expect(screen.getByLabelText('名称')).toBeVisible();
    expect(call).not.toHaveBeenCalled();
  });
});
