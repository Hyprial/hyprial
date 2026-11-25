/*
 * Decision record (PR 1f/F6): a first-run action set has no fallback factory;
 * a port the composition lacks is simply absent. Tests build exactly the ports
 * they exercise through this helper, so a missing port is visible in the test.
 */
import type { FirstRunActions } from '../../src/features/onboarding';

export function stubFirstRunActions(overrides: FirstRunActions = {}): FirstRunActions {
  return { ...overrides };
}
