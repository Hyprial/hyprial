import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { InfoTip, Tooltip } from '../../src/components/primitives';

describe('Tooltip', () => {
  it('keeps content hidden by default and shows it on focus with aria-describedby', () => {
    render(
      <Tooltip content="补充说明">
        <button type="button">触发</button>
      </Tooltip>,
    );
    const trigger = screen.getByRole('button', { name: '触发' });
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    expect(tip).not.toBeNull();
    expect(tip.hidden).toBe(true);
    fireEvent.focus(trigger);
    expect(tip.hidden).toBe(false);
    expect(trigger.getAttribute('aria-describedby')).toBe(tip.id);
    expect(tip.textContent).toBe('补充说明');
  });

  it('hides on Escape and blur', () => {
    render(
      <Tooltip content="补充说明">
        <button type="button">触发</button>
      </Tooltip>,
    );
    const trigger = screen.getByRole('button', { name: '触发' });
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    fireEvent.focus(trigger);
    expect(tip.hidden).toBe(false);
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(tip.hidden).toBe(true);
    fireEvent.focus(trigger);
    fireEvent.blur(trigger);
    expect(tip.hidden).toBe(true);
  });

  it('shows on hover and hides on mouse leave', () => {
    render(
      <Tooltip content="补充说明">
        <button type="button">触发</button>
      </Tooltip>,
    );
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    const wrapper = tip.parentElement as HTMLElement;
    fireEvent.mouseEnter(wrapper);
    expect(tip.hidden).toBe(false);
    fireEvent.mouseLeave(wrapper);
    expect(tip.hidden).toBe(true);
  });
});

describe('InfoTip', () => {
  it('renders a button with an accessible name and toggles on click', () => {
    render(<InfoTip content="可选的补充解释" label="说明" />);
    const button = screen.getByRole('button', { name: '说明' });
    expect(button.getAttribute('type')).toBe('button');
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    expect(tip.hidden).toBe(true);
    fireEvent.click(button);
    expect(tip.hidden).toBe(false);
    expect(button.getAttribute('aria-describedby')).toBe(tip.id);
    fireEvent.click(button);
    expect(tip.hidden).toBe(true);
  });
});
