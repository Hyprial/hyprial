/*
 * Decision record (PR 1f/F2): which catalog methods each gateway action needs.
 * The live source and the composition ask this one table whether an action
 * would be refused before dispatch (METHOD_NOT_FOUND, WRITE_METHOD_UNVERIFIED,
 * ...), so a flow can notify before it opens a dialog or changes any state.
 * Actions that are not listed need no catalog method (self-started reads) or
 * are not implemented by the live source at all.
 * - Read tabs (PR 1f/F6): a tab built only on a detail read that the catalog
 *   refuses is not rendered. Each tab lists alternative read groups; it is
 *   available when any group is fully callable (成员 shows people from either
 *   orgfs.members or the org.network device directory). A read that was
 *   dispatched and failed is not a refusal and keeps its text.
 * - Unprovided tabs (PR 1f/F6): the live source never supplies 拓扑 or 统计
 *   data, so those tabs are not rendered (nor linked) in the formal build.
 *   A source without `provides` (fixtures, previews) keeps every tab.
 * - createEntry serves two controls: opening 新建 is refused only when neither
 *   entry kind can be created; submitting checks the chosen kind
 *   (createFolder -> orgfs.mkdir, createFile -> orgfs.write).
 */
import { accessRefusal, type MethodAccess, type MethodRefusal } from '../../../../client';
import type { OrgGuardKey } from '../domain/unwired';
import type { OrgTab } from '../domain/types';

const CATALOG_METHODS: Readonly<Partial<Record<OrgGuardKey, readonly string[]>>> = {
  refresh: ['org.list'],
  syncContext: ['org.fetch'],
  createOrg: ['org.create'],
  previewDelete: ['org.delete'],
  deleteOrg: ['org.delete'],
  previewRepair: ['org.repair'],
  repairOrg: ['org.repair'],
  createInvite: ['org.invite'],
  joinByLink: ['org.join'],
  previewLeave: ['org.leave'],
  leaveOrg: ['org.leave'],
  previewRemoveMember: ['org.remove'],
  removeMember: ['org.remove'],
  retryMachine: ['org.publish'],
  saveFile: ['orgfs.write'],
  createFolder: ['orgfs.mkdir'],
  createFile: ['orgfs.write'],
  moveEntry: ['orgfs.move'],
  deleteEntry: ['orgfs.remove'],
  restoreEntry: ['orgfs.restore'],
};

const TAB_READS: Readonly<Partial<Record<OrgTab, readonly (readonly string[])[]>>> = {
  members: [['orgfs.members'], ['org.network']],
  machines: [['org.network']],
  files: [['orgfs.spaces', 'orgfs.ls', 'orgfs.read']],
};

const LIVE_UNPROVIDED_TABS: ReadonlySet<OrgTab> = new Set<OrgTab>(['topology', 'stats']);

export function orgTabProvided(tab: OrgTab): boolean {
  return !LIVE_UNPROVIDED_TABS.has(tab);
}

export function orgTabRefusal(
  access: MethodAccess | undefined,
  tab: OrgTab,
): MethodRefusal | null {
  const groups = TAB_READS[tab];
  if (!groups) return null;
  let refusal: MethodRefusal | null = null;
  for (const group of groups) {
    refusal = null;
    for (const method of group) {
      refusal = accessRefusal(access, method);
      if (refusal !== null) break;
    }
    if (refusal === null) return null;
  }
  return refusal;
}

export function orgMethodRefusal(
  access: MethodAccess | undefined,
  method: OrgGuardKey,
): MethodRefusal | null {
  if (method === 'createEntry') {
    const folder = orgMethodRefusal(access, 'createFolder');
    const file = orgMethodRefusal(access, 'createFile');
    return folder !== null && file !== null ? file : null;
  }
  for (const catalogMethod of CATALOG_METHODS[method] ?? []) {
    const refusal = accessRefusal(access, catalogMethod);
    if (refusal !== null) return refusal;
  }
  return null;
}
