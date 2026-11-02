/**
 * J6 machine list view model (product side).
 *
 * This module holds product types, product copy and the two product-only
 * models a page may render. Development seeds and their builder live in
 * `src/dev-mode/machine-scenarios.ts`, so no synthetic hostname can reach the
 * production bundle through the page's import graph.
 *
 * The machine section renders only from this model. It never reads the OS,
 * never mints a device identity, never sends a request and never writes storage.
 */

export type MachineScene = 'unavailable' | 'hidden' | 'm14' | 'm15' | 'linked';

export type MachineConnection = 'online' | 'offline';

export interface MachineRecord {
  readonly id: string;
  readonly hostname: string;
  readonly system: string;
  readonly joinedAt: string;
  readonly connection: MachineConnection;
  readonly current: boolean;
}

export interface MachineSectionModel {
  readonly scene: MachineScene;
  readonly title: string;
  readonly machines: readonly MachineRecord[];
  readonly emptyText: string | null;
  readonly unavailableNote: string | null;
  readonly currentLabel: string;
}

export const MACHINE_CONNECTION_LABELS: Readonly<Record<MachineConnection, string>> =
  {
    online: '在线',
    offline: '离线',
  };

export const MACHINE_TITLE = '机器';
export const MACHINE_CURRENT_LABEL = '这台电脑';
export const MACHINE_UNAVAILABLE_NOTE = '机器网络暂不可用';
export const MACHINE_EMPTY_TEXT = '还没有机器记录';

const BASE = {
  title: MACHINE_TITLE,
  machines: [],
  emptyText: null,
  unavailableNote: null,
  currentLabel: MACHINE_CURRENT_LABEL,
} as const;

/** Product default: no machine interface is wired, so nothing is listed. */
export function buildUnavailableMachineSectionModel(): MachineSectionModel {
  return {
    ...BASE,
    scene: 'unavailable',
    unavailableNote: MACHINE_UNAVAILABLE_NOTE,
  };
}

/** Used when the organization join failed, so the section must not render. */
export function buildHiddenMachineSectionModel(): MachineSectionModel {
  return { ...BASE, scene: 'hidden' };
}
