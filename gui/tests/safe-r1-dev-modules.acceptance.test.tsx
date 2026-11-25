import { createElement, type ComponentType } from 'react';
import { render, screen, within } from '@testing-library/react';

interface DevModuleIndexProps {
  readonly enabled?: boolean;
}

async function loadDevModuleIndex(): Promise<
  ComponentType<DevModuleIndexProps>
> {
  const modulePath = '../src/dev/DevModuleIndex.tsx';
  let loaded: {
    readonly DevModuleIndex: ComponentType<DevModuleIndexProps>;
  };
  try {
    loaded = (await import(modulePath)) as typeof loaded;
  } catch {
    expect.fail('DevModuleIndex is missing from the SAFE-R1 baseline');
  }
  return loaded.DevModuleIndex;
}

describe('safe R1 development module index', () => {
  it('lists each functional module once with a non-interactive unavailable state', async () => {
    const DevModuleIndex = await loadDevModuleIndex();
    render(createElement(DevModuleIndex, { enabled: true }));

    const moduleList = screen.getByRole('list', {
      name: '开发模块索引',
    });
    const expected = Array.from(
      { length: 6 },
      (_, index) => `dev-0${index + 1}`,
    );
    const entries = Array.from(
      moduleList.querySelectorAll<HTMLElement>('[data-dev-module]'),
    );

    expect(entries.map((entry) => entry.dataset.devModule)).toEqual(expected);
    expect(new Set(entries.map((entry) => entry.dataset.devModule)).size).toBe(6);
    for (const moduleId of expected) {
      const entry = moduleList.querySelector<HTMLElement>(
        `[data-dev-module="${moduleId}"]`,
      );
      expect(entry).not.toBeNull();
      expect(entry).toHaveTextContent(moduleId);
      expect(entry).not.toHaveTextContent('不可用');
    }
    expect(within(moduleList).queryAllByRole('button')).toHaveLength(0);
    expect(within(moduleList).queryAllByRole('link')).toHaveLength(0);
    expect(entries.every((entry) => !entry.hasAttribute('tabindex'))).toBe(true);
  });

  it('renders the index without a component-level build switch, gating moved to the mount point', async () => {
    const DevModuleIndex = await loadDevModuleIndex();
    render(createElement(DevModuleIndex, {}));

    expect(
      screen.getByRole('list', { name: '开发模块索引' }),
    ).toBeInTheDocument();
    expect(document.querySelectorAll('[data-dev-module]')).toHaveLength(6);
  });
});
