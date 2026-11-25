// 编译期契约：protected sectionProps 不可传入（tsc 会检查 @ts-expect-error）。
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ModuleSection } from '../../../src/components/primitives';

describe('ModuleSection compile-time contract', () => {
  it('rejects protected section props at the type level', () => {
    // @ts-expect-error id 由组件契约持有，不能经 sectionProps 覆盖
    const withId = <ModuleSection title="a" sectionProps={{ id: 'boom' }} />;
    // @ts-expect-error aria-labelledby 由组件契约持有
    const withAria = <ModuleSection title="a" sectionProps={{ 'aria-labelledby': 'hijack' }} />;
    // @ts-expect-error data-module 是受保护契约
    const withModule = <ModuleSection title="a" sectionProps={{ 'data-module': 'hijacked' }} />;
    // @ts-expect-error data-busy 是受保护契约
    const withBusy = <ModuleSection title="a" sectionProps={{ 'data-busy': 'true' }} />;
    expect([withId, withAria, withModule, withBusy]).toHaveLength(4);
  });

  it('keeps data-busy out of the DOM even if forced at runtime', () => {
    const { container } = render(
      <ModuleSection
        title="模型服务"
        sectionProps={{ 'data-busy': 'true' } as never}
      />,
    );
    const module = container.querySelector('[data-module]')!;
    expect(module.hasAttribute('data-busy')).toBe(false);
  });
});
