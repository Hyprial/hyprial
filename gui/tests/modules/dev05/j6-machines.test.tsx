import { render, screen, within } from '@testing-library/react';
import { vi } from 'vitest';
import { OrgMachines } from '../../../src/features/network/impl/OrgMachines';
import {
  buildHiddenMachineSectionModel,
  type MachineSectionModel,
} from '../../../src/features/network/impl/machine-view-model';
import {
  buildMachineScenario,
  M14_MACHINES,
} from '../../../src/dev/machine-scenarios';

function sceneModel(scene: 'm14' | 'm15'): MachineSectionModel {
  const model = buildMachineScenario(scene);
  if (!model) {
    throw new Error(`missing machine scenario ${scene}`);
  }
  return model;
}

const OFFLINE_MODEL: MachineSectionModel = {
  ...sceneModel('m14'),
  machines: [
    {
      id: 'studio-mac',
      hostname: 'studio-mac',
      system: 'macOS 合成系统',
      joinedAt: '2026-10-20 09:30',
      connection: 'online',
      current: true,
    },
    {
      id: 'travel-mac',
      hostname: 'travel-mac',
      system: 'macOS 合成系统',
      joinedAt: '2026-10-21 14:05',
      connection: 'offline',
      current: false,
    },
  ],
};

describe('J6 machine section model', () => {
  it('builds two synthetic machines for the M-14 scene', () => {
    const model = sceneModel('m14');

    expect(model.scene).toBe('m14');
    expect(model.machines).toHaveLength(2);
    expect(M14_MACHINES).toHaveLength(2);
    expect(model.machines.filter((machine) => machine.current)).toHaveLength(1);
    expect(model.emptyText).toBeNull();
  });

  it('marks the M-15 scene as joined but empty', () => {
    const model = sceneModel('m15');

    expect(model.scene).toBe('m15');
    expect(model.machines).toHaveLength(0);
    expect(model.emptyText).toBe('还没有机器记录');
  });

  it('renders nothing for the hidden scene', () => {
    const model = buildHiddenMachineSectionModel();
    const { container } = render(<OrgMachines model={model} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('J6 machine section rendering', () => {
  it('lists two machines with hostname, system, join time and connection state', () => {
    render(<OrgMachines model={sceneModel('m14')} />);

    const list = screen.getByRole('list', { name: '机器列表' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(2);

    expect(within(list).getByText('studio-mac')).toBeInTheDocument();
    expect(within(list).getByText('travel-mac')).toBeInTheDocument();
    expect(within(list).getAllByText('这台电脑')).toHaveLength(1);
    expect(within(list).getAllByText('在线').length).toBeGreaterThan(0);
    expect(within(list).getAllByText(/合成系统/).length).toBe(2);
    expect(within(list).getAllByText(/2026-10/).length).toBe(2);
  });

  it('never renders a remove action for the current machine', () => {
    render(<OrgMachines model={sceneModel('m14')} />);

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText('移除')).not.toBeInTheDocument();
  });

  it('shows the offline state for a non-current machine', () => {
    render(<OrgMachines model={OFFLINE_MODEL} />);

    expect(screen.getAllByText('离线')).toHaveLength(1);
    expect(screen.getByText('这台电脑')).toBeInTheDocument();
  });

  it('shows the empty text when the joined organization has no machines', () => {
    render(<OrgMachines model={sceneModel('m15')} />);

    expect(screen.getByText('还没有机器记录')).toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('renders without requests or storage writes', () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    render(<OrgMachines model={sceneModel('m14')} />);

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    vi.unstubAllGlobals();
  });
});
