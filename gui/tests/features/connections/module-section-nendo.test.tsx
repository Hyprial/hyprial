// D-NENDO-007：ModuleSection 组件契约（标题/说明/动作/内容都在同一 surface 内）。
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button, ModuleSection } from '../../../src/components/primitives';

describe('ModuleSection', () => {
  it('keeps title, description, actions and body inside one labelled surface', async () => {
    const onAction = vi.fn();
    const { container } = render(
      <ModuleSection
        id="demo-title"
        title="模型服务"
        description="说明文字"
        actions={<button type="button" onClick={onAction}>添加服务</button>}
      >
        <p>模块内容</p>
      </ModuleSection>,
    );
    const module = container.querySelector('[data-module]');
    expect(module).not.toBeNull();
    expect(module?.getAttribute('aria-labelledby')).toBe('demo-title');
    const heading = screen.getByRole('heading', { level: 2, name: '模型服务' });
    expect(module?.contains(heading)).toBe(true);
    expect(module?.contains(screen.getByText('说明文字'))).toBe(true);
    expect(module?.contains(screen.getByRole('button', { name: '添加服务' }))).toBe(true);
    expect(module?.contains(screen.getByText('模块内容'))).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: '添加服务' }));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it('resolves aria-labelledby to exactly one heading', () => {
    render(<ModuleSection title="渠道状态" />);
    const module = document.querySelector('[data-module]')!;
    const id = module.getAttribute('aria-labelledby')!;
    expect(document.querySelectorAll(`#${CSS.escape(id)}`)).toHaveLength(1);
    expect(document.querySelector(`#${CSS.escape(id)}`)?.tagName).toBe('H2');
  });

  it('passes arbitrary action controls through without rewriting props', async () => {
    const onClick = vi.fn();
    render(
      <ModuleSection
        title="服务商"
        actions={(
          <Button
            variant="ghost"
            aria-label="登录服务商（aria）"
            loading
            onClick={onClick}
          >
            登录服务商
          </Button>
        )}
      />,
    );
    const button = screen.getByRole('button', { name: '登录服务商（aria）' });
    expect(button.className).toContain('ui-button--ghost');
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect(button).toBeDisabled(); // loading 由原控件自己决定
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('cannot be hijacked through sectionProps', () => {
    const { container } = render(
      <ModuleSection
        id="real-title"
        title="模型服务"
        description="真实说明"
        sectionProps={{
          // 故意传入契约字段（类型层禁止，运行时也必须被后置值覆盖）
          id: 'boom',
          'aria-labelledby': 'hijack',
          'data-module': 'hijacked',
          role: 'region',
          'aria-describedby': 'module-desc',
        } as never}
      >
        <p>内容</p>
      </ModuleSection>,
    );
    const module = container.querySelector('[data-module]')!;
    expect(module.getAttribute('data-module')).toBe('');
    expect(module.getAttribute('aria-labelledby')).toBe('real-title');
    expect(module.id).not.toBe('boom');
    expect(document.querySelectorAll('#real-title')).toHaveLength(1);
    expect(document.querySelector('#real-title')?.tagName).toBe('H2');
    // 合法属性仍透传
    expect(module.getAttribute('role')).toBe('region');
    expect(module.getAttribute('aria-describedby')).toBe('module-desc');
  });

  it('forwards a ref to the single surface element', () => {
    const ref = { current: null as HTMLElement | null };
    render(<ModuleSection title="渠道状态" sectionRef={ref} />);
    expect(ref.current?.tagName).toBe('SECTION');
    expect(ref.current?.getAttribute('data-module')).toBe('');
  });

  it('renders exactly one card surface with the module attributes', () => {
    const { container } = render(
      <ModuleSection title="模型服务" sectionProps={{ role: 'region' }}>
        <p>内容</p>
      </ModuleSection>,
    );
    const module = container.querySelector('[data-module]')!;
    expect(module.classList.contains('ui-card')).toBe(true);
    expect(module.classList.contains('ui-module')).toBe(true);
    expect(module.getAttribute('role')).toBe('region');
    expect(container.querySelectorAll('.ui-card')).toHaveLength(1);
    expect(module.querySelector('.ui-card')).toBeNull();
  });
});
