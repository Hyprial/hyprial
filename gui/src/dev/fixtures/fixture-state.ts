/**
 * Decision record (GUI deep modules C2, 2026-11-08): default fixture data lives
 * in this development module. The formal state module provides only the
 * data-driven state builder; development compositions install
 * `fixtureAppReducer` to keep the historical fixture reset on logout.
 */
import { FIXTURE_AGENTS, FIXTURE_CONNECTIONS, FIXTURE_THREADS } from './demo';
import {
  appReducer,
  createStateFromBaseline,
  type StateBaseline,
  LOGOUT_NOTICE,
} from '../../state/appReducer';
import type {
  AppAction,
  AppState,
  ChatSeed,
  FirstRunMode,
} from '../../state/types';

export const DEFAULT_TARGET_ID = 'agent-squire';

export function createFixtureState(
  firstRunMode?: FirstRunMode,
  notice?: string | null,
  chatSeed?: ChatSeed,
): AppState;
export function createFixtureState(
  baseline: StateBaseline,
  chatSeed?: ChatSeed,
  firstRunMode?: FirstRunMode,
): AppState;
export function createFixtureState(
  baseline: StateBaseline,
  firstRunMode?: FirstRunMode,
  notice?: string | null,
  chatSeed?: ChatSeed,
): AppState;
export function createFixtureState(
  firstRunModeOrBaseline: FirstRunMode | StateBaseline = 'complete',
  noticeOrSeed: string | ChatSeed | null = null,
  seedOrMode: ChatSeed | string | null = {},
  chatSeed?: ChatSeed,
): AppState {
  const seed = typeof noticeOrSeed === 'object' && noticeOrSeed !== null
    ? noticeOrSeed
    : typeof seedOrMode === 'object' && seedOrMode !== null ? seedOrMode : {};
  if (typeof firstRunModeOrBaseline === 'string') {
    const state = createStateFromBaseline(
      {
        targets: FIXTURE_AGENTS,
        connections: FIXTURE_CONNECTIONS,
        threads: FIXTURE_THREADS,
      },
      seed,
      firstRunModeOrBaseline,
    );
    return { ...state, notice: typeof noticeOrSeed === 'string' ? noticeOrSeed : null };
  }
  const state = createStateFromBaseline(
    firstRunModeOrBaseline,
    chatSeed ?? seed,
    typeof noticeOrSeed === 'string'
      ? noticeOrSeed as FirstRunMode
      : typeof seedOrMode === 'string' ? seedOrMode as FirstRunMode : 'complete',
  );
  return { ...state, notice: typeof seedOrMode === 'object' && seedOrMode !== null ? null : seedOrMode };
}

/**
 * Development reducer wrapper for fixture/live fallback compositions: logout
 * keeps resetting to the fixture baseline, exactly as before C2. Notice ids
 * keep coming from the formal allocator through the synthetic push-notice.
 */
export function fixtureAppReducer(state: AppState, action: AppAction): AppState {
  if (action.type === 'confirm-logout') {
    const fresh = appReducer(
      createFixtureState('complete', null),
      { type: 'push-notice', tone: 'info', text: LOGOUT_NOTICE },
    );
    return { ...fresh, notice: LOGOUT_NOTICE };
  }
  return appReducer(state, action);
}
