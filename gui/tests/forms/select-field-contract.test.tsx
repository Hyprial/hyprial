// Select/Field 可访问性契约测试（PR #2579 交接 C2–C4，见
// docs/design/gui-testing-architecture-handoff-2026-11-08.md §2.1/§4–5）。
// 只经公共 primitives 入口测真实组件：不 mock Select/Field，不用匿名 role
// 查询，不用“非空”弱断言。基线 486c1711 上 C2/C3/C4 断言预期红（WIP 先红
// 后绿）；ref、受控值与空值哨兵为回归护栏，预期绿。jsdom 缺少 Radix 弹层
// 依赖的 pointer capture / scrollIntoView API，beforeAll 补齐。
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createRef, useState } from 'react';
import { Field, Select } from '../../src/components/primitives';

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

function describedByIds(element: HTMLElement): string[] {
  return (element.getAttribute('aria-describedby') ?? '')
    .split(' ')
    .filter(Boolean);
}

describe('Select 可访问名称与属性透传（C2）', () => {
  it('aria-label 成为 combobox 的可访问名称', () => {
    render(
      <Select
        aria-label="选择 harness"
        value="claude"
        options={OPTIONS}
        onValueChange={() => {}}
      />,
    );
    expect(
      screen.getByRole('combobox', { name: '选择 harness' }),
    ).toBeInTheDocument();
  });

  it('aria-labelledby 引用外部元素作为可访问名称', () => {
    render(
      <>
        <span id="harness-name-source">外部 harness 标签</span>
        <Select
          aria-labelledby="harness-name-source"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </>,
    );
    expect(
      screen.getByRole('combobox', { name: '外部 harness 标签' }),
    ).toBeInTheDocument();
  });

  it('aria-describedby 到达 trigger 并构成可访问描述', () => {
    render(
      <>
        <label htmlFor="desc-select">harness 描述来源</label>
        <Select
          id="desc-select"
          aria-describedby="harness-caller-desc"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
        <p id="harness-caller-desc">调用方提供的说明</p>
      </>,
    );
    expect(
      screen.getByRole('combobox', { name: 'harness 描述来源' }),
    ).toHaveAccessibleDescription('调用方提供的说明');
  });

  it('disabled 时仍具可访问名称且不打开弹出层', async () => {
    const user = userEvent.setup();
    render(
      <Select
        aria-label="禁用 harness"
        value="claude"
        options={OPTIONS}
        onValueChange={() => {}}
        disabled
      />,
    );
    const trigger = screen.getByRole('combobox', { name: '禁用 harness' });
    expect(trigger).toBeDisabled();
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('ref 转发到 trigger 按钮元素', () => {
    const ref = createRef<HTMLButtonElement>();
    render(
      <>
        <label htmlFor="ref-select">ref harness</label>
        <Select
          id="ref-select"
          ref={ref}
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </>,
    );
    expect(ref.current).toBe(screen.getByRole('combobox', { name: 'ref harness' }));
  });
});

describe('Field 注入 composite Select 的错误与 required 语义（C3）', () => {
  it('错误状态：aria-invalid、aria-errormessage 与 hint/error 描述到达 trigger', () => {
    render(
      <Field
        label="harness"
        htmlFor="error-harness"
        hint="选择参与对话的 harness"
        error="此选项不可用"
      >
        <Select
          id="error-harness"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </Field>,
    );
    const trigger = screen.getByRole('combobox', { name: /harness/ });
    expect(trigger).toHaveAttribute('aria-invalid', 'true');
    expect(trigger).toHaveAttribute('aria-errormessage', 'error-harness-error');
    expect(trigger).toHaveAccessibleDescription(/选择参与对话的 harness/);
    expect(trigger).toHaveAccessibleDescription(/此选项不可用/);
  });

  it('required 语义：trigger 暴露 aria-required', () => {
    render(
      <Field label="harness" htmlFor="required-harness" required>
        <Select
          id="required-harness"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </Field>,
    );
    expect(screen.getByRole('combobox', { name: /harness/ })).toHaveAttribute(
      'aria-required',
      'true',
    );
  });

  it('错误清除后恢复：invalid/errormessage 移除，hint 描述保留', () => {
    const { rerender } = render(
      <Field
        label="harness"
        htmlFor="recover-harness"
        hint="选择参与对话的 harness"
        error="此选项不可用"
      >
        <Select
          id="recover-harness"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </Field>,
    );
    const trigger = screen.getByRole('combobox', { name: /harness/ });
    expect(trigger).toHaveAttribute('aria-invalid', 'true');

    rerender(
      <Field label="harness" htmlFor="recover-harness" hint="选择参与对话的 harness">
        <Select
          id="recover-harness"
          value="claude"
          options={OPTIONS}
          onValueChange={() => {}}
        />
      </Field>,
    );
    expect(trigger).not.toHaveAttribute('aria-invalid');
    expect(trigger).not.toHaveAttribute('aria-errormessage');
    expect(trigger).toHaveAccessibleDescription('选择参与对话的 harness');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('Field 与调用方 aria-describedby 合并去重（C4）', () => {
  it('不丢失调用方已有描述，并与 hint ID 并存', () => {
    render(
      <>
        <p id="caller-desc">调用方提供的说明</p>
        <Field label="名称" htmlFor="merge-name" hint="字段提示">
          <input id="merge-name" aria-describedby="caller-desc" />
        </Field>
      </>,
    );
    const input = screen.getByLabelText(/名称/);
    expect(describedByIds(input)).toEqual(
      expect.arrayContaining(['caller-desc', 'merge-name-hint']),
    );
    expect(input).toHaveAccessibleDescription(/调用方提供的说明/);
  });

  it('与 hint/error ID 合并且去重，调用方引用同一 ID 时不重复', () => {
    render(
      <>
        <p id="caller-desc">调用方提供的说明</p>
        <Field
          label="名称"
          htmlFor="dedupe-name"
          hint="字段提示"
          error="名称不能为空"
        >
          <input
            id="dedupe-name"
            aria-describedby="caller-desc dedupe-name-hint"
          />
        </Field>
      </>,
    );
    const ids = describedByIds(screen.getByLabelText(/名称/));
    expect(ids).toEqual(
      expect.arrayContaining([
        'caller-desc',
        'dedupe-name-hint',
        'dedupe-name-error',
      ]),
    );
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('Field + Select 组合：调用方描述与错误同时到达（C2+C3+C4）', () => {
  it('trigger 同时暴露错误状态、调用方说明与 Field 错误', () => {
    render(
      <>
        <p id="combined-caller-desc">调用方提供的说明</p>
        <Field
          label="harness"
          htmlFor="combined-harness"
          hint="选择参与对话的 harness"
          error="此选项不可用"
        >
          <Select
            id="combined-harness"
            aria-describedby="combined-caller-desc"
            value="claude"
            options={OPTIONS}
            onValueChange={() => {}}
          />
        </Field>
      </>,
    );
    const trigger = screen.getByRole('combobox', { name: /harness/ });
    expect(trigger).toHaveAttribute('aria-invalid', 'true');
    expect(describedByIds(trigger)).toContain('combined-caller-desc');
    expect(trigger).toHaveAccessibleDescription(/调用方提供的说明/);
    expect(trigger).toHaveAccessibleDescription(/此选项不可用/);
  });
});

describe('Select 受控值与空值哨兵语义', () => {
  it('空值哨兵精确往返：回调序列恰为 claude → 空字符串', async () => {
    const user = userEvent.setup();
    const changes: string[] = [];
    function Harness() {
      const [value, setValue] = useState('');
      return (
        <>
          <label htmlFor="empty-select">首选 harness</label>
          <Select
            id="empty-select"
            value={value}
            options={OPTIONS}
            onValueChange={(next) => {
              changes.push(next);
              setValue(next);
            }}
          />
          <output data-testid="empty-value">{value}</output>
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole('combobox', { name: '首选 harness' });
    expect(trigger).toHaveTextContent('未指定');
    expect(screen.getByTestId('empty-value')).toBeEmptyDOMElement();

    await user.click(trigger);
    await user.click(screen.getByRole('option', { name: 'claude' }));
    expect(changes).toEqual(['claude']);
    expect(screen.getByTestId('empty-value')).toHaveTextContent('claude');

    await user.click(trigger);
    await user.click(screen.getByRole('option', { name: '未指定' }));
    expect(changes).toEqual(['claude', '']);
    expect(screen.getByTestId('empty-value')).toBeEmptyDOMElement();
    expect(trigger).toHaveTextContent('未指定');
  });

  it('受控值语义：调用方不更新 value 时显示不漂移', async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    render(
      <>
        <label htmlFor="strict-select">严格受控 harness</label>
        <Select
          id="strict-select"
          value="claude"
          options={OPTIONS}
          onValueChange={onValueChange}
        />
      </>,
    );
    const trigger = screen.getByRole('combobox', { name: '严格受控 harness' });
    expect(trigger).toHaveTextContent('claude');
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.click(screen.getByRole('option', { name: 'codex' }));
    expect(onValueChange).toHaveBeenCalledTimes(1);
    expect(onValueChange).toHaveBeenCalledWith('codex');
    expect(trigger).toHaveTextContent('claude');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });
});
