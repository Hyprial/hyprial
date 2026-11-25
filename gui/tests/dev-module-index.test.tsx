import { render, screen } from '@testing-library/react';
import {
  DEV_MODULES,
  DevModuleIndex,
} from '../src/dev';

describe('development module index', () => {
  it('defines one stable owner boundary for dev-01 through dev-06', () => {
    expect(DEV_MODULES.map(({ id }) => id)).toEqual([
      'dev-01',
      'dev-02',
      'dev-03',
      'dev-04',
      'dev-05',
      'dev-06',
    ]);
    expect(new Set(DEV_MODULES.map(({ owner }) => owner)).size).toBe(6);
  });

  it('shows all six unavailable module entries without environment gating', () => {
    render(<DevModuleIndex />);

    expect(
      screen.getByRole('list', { name: '开发模块索引' }),
    ).toBeInTheDocument();
    for (const { id, name } of DEV_MODULES) {
      const entry = screen.getByTestId(`dev-module-${id}`);
      expect(entry).toHaveAttribute('data-dev-module', id);
      expect(entry).toHaveAttribute('role', 'listitem');
      expect(entry).toHaveTextContent(id);
      expect(entry).toHaveTextContent(name);
      expect(entry).not.toHaveTextContent('不可用');
      expect(entry).not.toHaveAttribute('tabindex');
    }
    expect(screen.getByText('横向滚动查看全部模块')).toBeInTheDocument();
  });
});
