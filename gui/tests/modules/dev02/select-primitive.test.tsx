import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Select } from '../../../src/components/primitives';

// Radix Select 在 jsdom 打开弹出层需要这些 API 存在（真实浏览器原生提供）。
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const OPTIONS = [
  { value: '', label: '未指定' },
  { value: 'claude', label: 'claude' },
  { value: 'codex', label: 'codex' },
];

function Harness({ initial = '' }: { readonly initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <label htmlFor="test-select">首选 harness</label>
      <Select
        id="test-select"
        value={value}
        options={OPTIONS}
        onValueChange={setValue}
      />
      <output data-testid="value">{value}</output>
    </>
  );
}

describe('Select 基件适配层', () => {
  it('keeps disabled options visible but skips them for mouse and keyboard selection', async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<Select aria-label="服务商" value="first" onValueChange={change} options={[
      { value: 'first', label: '已支持' },
      { value: 'custom', label: '停用选项', disabled: true },
      { value: 'next', label: '另一服务商' },
    ]} />);
    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: '停用选项' }));
    expect(change).not.toHaveBeenCalled();
    screen.getByRole('option', { name: '已支持' }).focus();
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(screen.getByRole('option', { name: '另一服务商' })).toHaveFocus());
    await user.keyboard('{Enter}');
    expect(change).toHaveBeenCalledWith('next');
    expect(change).not.toHaveBeenCalledWith('custom');
  });
  it('label 关联 trigger（combobox），空值选项展示其标签', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const trigger = screen.getByLabelText('首选 harness');
    expect(trigger).toHaveAttribute('role', 'combobox');
    expect(trigger).toHaveTextContent('未指定');
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(screen.getByRole('option', { name: 'codex' }));
    expect(trigger).toHaveTextContent('codex');
    expect(screen.getByTestId('value').textContent).toBe('codex');
  });

  it('键盘：方向键打开并选择，Enter 确认，Esc 关闭并还原', async () => {
    const user = userEvent.setup();
    render(<Harness initial="claude" />);
    const trigger = screen.getByLabelText('首选 harness');
    trigger.focus();
    await user.keyboard('{ArrowDown}'); // 打开并把高亮移到下一项
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(trigger).toHaveTextContent('claude');
    expect(trigger).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    await user.keyboard('{Enter}');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(screen.getByTestId('value').textContent).not.toBe('');
  });

  it('空值经哨兵往返：选回空选项时回调收到空字符串', async () => {
    const user = userEvent.setup();
    render(<Harness initial="claude" />);
    await user.click(screen.getByLabelText('首选 harness'));
    await user.click(screen.getByRole('option', { name: '未指定' }));
    expect(screen.getByTestId('value').textContent).toBe('');
    expect(screen.getByLabelText('首选 harness')).toHaveTextContent('未指定');
  });

  it('disabled 时不打开弹出层', async () => {
    const user = userEvent.setup();
    render(
      <Select
        aria-label="禁用选择"
        value="claude"
        options={OPTIONS}
        onValueChange={() => {}}
        disabled
      />,
    );
    const trigger = screen.getByRole('combobox');
    expect(trigger).toBeDisabled();
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });
});
