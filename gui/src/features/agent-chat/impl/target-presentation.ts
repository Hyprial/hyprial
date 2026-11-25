/**
 * Decision record (chat UI clarity, 2026-11-16):
- A connected target carries the daemon target URI in `role`; the directory has
  no separate human-readable role. Repeating that address under the display
  name wraps it across three lines in the 248–280px target column and competes
  with the name that identifies the row. The list and heading therefore omit a
  role that is only the address, while the object information panel keeps the
  complete address so diagnostics are not hidden.
- The kind label is presentation copy for the target kind, never a name derived
  from a URI segment.
 */
import type { SessionTarget } from '../../../state/gui-view-model';

export function targetRoleLabel(target: SessionTarget): string | undefined {
  const role = target.role.trim();
  return role.length > 0 && role !== target.id ? role : undefined;
}

export function targetKindLabel(target: SessionTarget): string {
  if (target.kind === 'group') return '群聊';
  return target.kind === 'human' ? '联系人' : 'Agent';
}

export function targetSubtitle(target: SessionTarget): string {
  return targetRoleLabel(target) ?? targetKindLabel(target);
}
