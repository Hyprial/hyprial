import type { OrgCopyHandler } from '../../pages/org/org-view-model';

/**
 * Copy feedback for the organization invitation.
 *
 * The text is a fixed placeholder from the deterministic model, so a copy never
 * reaches a real service and never writes browser storage. A missing clipboard
 * API or a rejected permission reports failure instead of success.
 */
export function copyOrgInvitation(): OrgCopyHandler {
  return async (_kind, text) => {
    if (!text) {
      return false;
    }
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      return false;
    }
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  };
}
