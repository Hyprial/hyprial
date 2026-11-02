import {
  MACHINE_EMPTY_TEXT,
  MACHINE_TITLE,
  MACHINE_CURRENT_LABEL,
  type MachineRecord,
  type MachineSectionModel,
} from '../features/network/machine-view-model';

/**
 * J6 development scenarios.
 *
 * The synthetic machines live here, not in `src/features/network/`, so the
 * product page's import graph never contains them. Only the J6 selector and
 * tests import this module.
 */
export const J6_SCENARIOS = [
  { value: 'baseline', label: '基线（无机器数据）' },
  { value: 'm14', label: 'M-14 机器列表有 2 台' },
  { value: 'm15', label: 'M-15 机器数据为空' },
] as const;

export type J6Scenario = (typeof J6_SCENARIOS)[number]['value'];

export function isJ6Scenario(value: string): value is J6Scenario {
  return J6_SCENARIOS.some((option) => option.value === value);
}

export const M14_MACHINES: readonly MachineRecord[] = [
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
    connection: 'online',
    current: false,
  },
];

/** Baseline returns undefined so the page keeps its product default. */
export function buildMachineScenario(
  scenario: J6Scenario,
): MachineSectionModel | undefined {
  if (scenario === 'baseline') {
    return undefined;
  }
  const base = {
    title: MACHINE_TITLE,
    machines: [],
    emptyText: null,
    unavailableNote: null,
    currentLabel: MACHINE_CURRENT_LABEL,
  } as const;
  if (scenario === 'm14') {
    return { ...base, scene: 'm14', machines: M14_MACHINES };
  }
  return { ...base, scene: 'm15', emptyText: MACHINE_EMPTY_TEXT };
}
