// C1/C5 组合契约（docs/design/gui-testing-architecture-handoff-2026-11-08.md §2.1/§4）：
// Dialog 内嵌 Select 的 Esc 分层、焦点归还、选项回调与 pending/disabled 语义。
// 基线 486c171 上 C1 未修复：第一次 Esc 会同时关闭父 Dialog，相关用例按目标契约
// 写为红测试，先保存失败结果，等产品切片统一 portal/Esc/焦点策略后转绿。
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type FormEvent } from 'react';
import { Button, Dialog, Field, Select } from '../../src/components/primitives';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const HARNESS_OPTIONS = [
  { value: 'claude', label: 'claude' },
  { value: 'codex', label: 'codex' },
];

interface HarnessProps {
  readonly onClose: () => void;
  readonly onValueChange: (value: string) => void;
  readonly onSave?: () => void;
  readonly pending?: boolean;
  readonly selectDisabled?: boolean;
}

function Harness({
  onClose,
  onValueChange,
  onSave,
  pending = false,
  selectDisabled = false,
}: HarnessProps) {
  const [open, setOpen] = useState(false);
  const [harness, setHarness] = useState('claude');
  const [name, setName] = useState('alpha');

  return (
    <>
      <Button onClick={() => setOpen(true)}>打开表单</Button>
      <Dialog
        open={open}
        title="接入设置"
        description="调整默认 harness 与展示名称"
        onClose={() => {
          onClose();
          setOpen(false);
        }}
        actions={[
          {
            label: '保存',
            variant: 'primary',
            loading: pending,
            onClick: onSave ?? (() => {}),
          },
        ]}
      >
        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
          }}
        >
          <Field label="名称" htmlFor="contract-name">
            <input
              id="contract-name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </Field>
          <div>
            <label htmlFor="contract-harness">harness</label>
            <Select
              id="contract-harness"
              value={harness}
              options={HARNESS_OPTIONS}
              onValueChange={(next) => {
                onValueChange(next);
                setHarness(next);
              }}
              disabled={selectDisabled}
            />
          </div>
        </form>
      </Dialog>
    </>
  );
}

describe('Dialog + Select 组合契约（C1/C5）', () => {
  it('第一次 Esc 只关闭菜单：父 Dialog 保留、表单值不变、焦点回 Select trigger', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onValueChange = vi.fn();
    render(
      <Harness onClose={onClose} onValueChange={onValueChange} />,
    );

    await user.click(screen.getByRole('button', { name: '打开表单' }));
    const trigger = screen.getByLabelText('harness');
    await user.click(trigger);
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('listbox')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByRole('dialog', { name: '接入设置' }),
    ).toBeVisible();
    expect(screen.getByLabelText('名称')).toHaveValue('alpha');
    expect(trigger).toHaveTextContent('claude');
    expect(trigger).toHaveFocus();
  });

  it('第二次 Esc 才关闭 Dialog：onClose 恰好一次，焦点回真实打开按钮', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onValueChange = vi.fn();
    render(
      <Harness onClose={onClose} onValueChange={onValueChange} />,
    );

    const openButton = screen.getByRole('button', { name: '打开表单' });
    await user.click(openButton);
    await user.click(screen.getByLabelText('harness'));

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(
      screen.getByRole('dialog', { name: '接入设置' }),
    ).toBeVisible();

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole('dialog', { name: '接入设置' }),
    ).toBeNull();
    expect(openButton).toHaveFocus();
  });

  it('点击选项确实切换到 codex 且回调恰好一次', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onValueChange = vi.fn();
    render(
      <Harness onClose={onClose} onValueChange={onValueChange} />,
    );

    await user.click(screen.getByRole('button', { name: '打开表单' }));
    const trigger = screen.getByLabelText('harness');
    await user.click(trigger);

    await user.click(screen.getByRole('option', { name: 'codex' }));

    expect(onValueChange).toHaveBeenCalledTimes(1);
    expect(onValueChange).toHaveBeenNthCalledWith(1, 'codex');
    expect(trigger).toHaveTextContent('codex');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(
      screen.getByRole('dialog', { name: '接入设置' }),
    ).toBeVisible();
  });

  it('pending 保存动作禁用防重复，表单其余控件不受影响', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onValueChange = vi.fn();
    const onSave = vi.fn();
    render(
      <Harness
        onClose={onClose}
        onValueChange={onValueChange}
        onSave={onSave}
        pending
      />,
    );

    await user.click(screen.getByRole('button', { name: '打开表单' }));
    const save = screen.getByRole('button', { name: '保存' });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute('aria-busy', 'true');

    await user.click(save);
    expect(onSave).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText('名称'), '-draft');
    expect(screen.getByLabelText('名称')).toHaveValue('alpha-draft');
  });

  it('disabled Select 在 Dialog 内不打开菜单', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onValueChange = vi.fn();
    render(
      <Harness
        onClose={onClose}
        onValueChange={onValueChange}
        selectDisabled
      />,
    );

    await user.click(screen.getByRole('button', { name: '打开表单' }));
    const trigger = screen.getByLabelText('harness');
    expect(trigger).toBeDisabled();

    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(
      screen.getByRole('dialog', { name: '接入设置' }),
    ).toBeVisible();
  });
});
