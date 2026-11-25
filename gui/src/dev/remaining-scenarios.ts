import type { LinkedSession } from './session/session-types';
import { createInitialSession } from './session/initial-session';

/**
 * Remaining-journey development switches.
 *
 * Only development compositions read this module; the product entry never does.
 */
export type J10Scenario = 'ready' | 'add-failure' | 'empty';

export function isJ10Scenario(value: string): value is J10Scenario {
  return value === 'ready' || value === 'add-failure' || value === 'empty';
}

export function applyJ10Scenario(
  session: LinkedSession,
  scenario: J10Scenario,
): LinkedSession {
  if (scenario === 'add-failure') {
    return { ...session, modelAddMode: 'failure' };
  }
  if (scenario === 'empty') {
    return { ...session, modelServiceSeed: 'empty' };
  }
  return session;
}

export function createRemainingSession(scenario: J10Scenario): LinkedSession {
  return applyJ10Scenario(createInitialSession(), scenario);
}

/** J4 development switches: ready, authorize failure and unavailable. */
export type J4Scenario = 'ready' | 'authorize-failure' | 'unavailable';

export function isJ4Scenario(value: string): value is J4Scenario {
  return value === 'ready' || value === 'authorize-failure' || value === 'unavailable';
}

export function applyJ4Scenario(
  session: LinkedSession,
  scenario: J4Scenario,
): LinkedSession {
  if (scenario === 'ready') {
    return session;
  }
  return {
    ...session,
    flowScenarios: { ...session.flowScenarios, feishu: scenario },
  };
}
