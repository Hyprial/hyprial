/*
 * Decision record (PR 1f/F6, S2d 2026-11-22): the first-run wizard is offered
 * only when every step it would show has the port that completes it: account
 * login (步骤 1), device initialization (步骤 2, after login), and runtime
 * setup (步骤 3: saveModelKey or configureAgent). The conversation step uses
 * the reply confirmation or chat controller, which the shell composes
 * separately. A composition that states `firstRunWizardEnabled` keeps its own
 * choice; only the absent value is derived here, so a missing port lands the
 * first-run route on Start instead of a step with no forward control.
 */
import type { FirstRunActions } from './types';

export function firstRunWizardOffered(actions: FirstRunActions | undefined): boolean {
  return Boolean(actions
    && typeof actions.authenticateAccount === 'function'
    && typeof actions.initializeDevice === 'function'
    && (typeof actions.saveModelKey === 'function' || typeof actions.configureAgent === 'function'));
}
