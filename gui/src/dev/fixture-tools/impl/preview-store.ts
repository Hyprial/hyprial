/*
 * Decision record (WP2 store, gui-dev-product-parity-2026-11-09; dev tools
 * seam, PR 4a, 2026-11-14):
 * - The development preview store (scenario snapshot + linked session) is
 *   fixture composition state. It moved out of DevPreviewApp unchanged so the
 *   floating tool layer no longer owns or imports it; the fixture composition
 *   root and the fixture tool contributions are its only consumers.
 * - The scenario URL mirror (`hyprialScenario`, legacy `hyprialFixture`
 *   removal), the two account/initialisation presets and the session reset
 *   on a preset change keep the behaviour introduced by #2727.
 */
import type { J1Scenario } from '../../DevScenarioSelector';
import type { J3Scenario } from '../../chat-scenarios';
import type { J2Scenario } from '../../j2-scenarios';
import type { J5Scenario } from '../../org-scenarios';
import type { J6Scenario } from '../../machine-scenarios';
import type { J7Scenario } from '../../topology-scenarios';
import type { OrgDevScenario } from '../../org/scenarios';
import { createInitialSession } from '../../session/initial-session';
import {
  applyJ10Scenario,
  applyJ4Scenario,
  type J10Scenario,
  type J4Scenario,
} from '../../remaining-scenarios';
import { sessionReducer } from '../../session/session-reducer';
import type { LinkedSession, SessionAction } from '../../session/session-types';

export interface DevScenarioState {
  readonly j1: J1Scenario;
  readonly j2: J2Scenario;
  readonly j3: J3Scenario;
  readonly j5: J5Scenario;
  readonly j6: J6Scenario;
  readonly j7: J7Scenario;
  readonly orgData: OrgDevScenario;
  readonly j4: J4Scenario;
  readonly j10: J10Scenario;
}

export interface DevPreviewStore {
  getScenario(): DevScenarioState;
  setScenario(patch: Partial<DevScenarioState>): void;
  subscribeScenario(listener: () => void): () => void;
  getSession(): LinkedSession;
  dispatchSession(action: SessionAction): void;
  resetSession(): void;
  subscribeSession(listener: () => void): () => void;
}

const PARITY_SCENARIO_PARAM = 'hyprialScenario';
const LEGACY_FIXTURE_PARAM = 'hyprialFixture';

const FIXTURE_DEFAULTS = {
  j1: 'simulate',
  j2: 'baseline',
  j3: 'baseline',
  j5: 'baseline',
  j6: 'baseline',
  j7: 'baseline',
  orgData: 'off',
} as const;

export type DevAuthInitState = 'logged-out-init' | 'logged-in-init';

type AuthInitPatch = Pick<DevScenarioState, 'j1' | 'j2' | 'j3' | 'j5' | 'j6' | 'j7' | 'orgData'>;

const AUTH_INIT_PATCHES: Readonly<Record<DevAuthInitState, AuthInitPatch>> = {
  'logged-out-init': {
    j1: 'logged-out-init',
    j2: 'm05',
    j3: 'baseline',
    j5: 'baseline',
    j6: 'baseline',
    j7: 'baseline',
    orgData: 'off',
  },
  'logged-in-init': {
    j1: 'logged-in-init',
    j2: 'm05',
    j3: 'baseline',
    j5: 'baseline',
    j6: 'baseline',
    j7: 'baseline',
    orgData: 'off',
  },
};

type FixtureScenarioKey = keyof typeof FIXTURE_DEFAULTS;

export const FIXTURE_PRESETS: readonly {
  readonly id: string;
  readonly label: string;
  readonly patch: Partial<Pick<DevScenarioState, FixtureScenarioKey>>;
}[] = [
  { id: 'logged-out-init', label: '未登录 · init', patch: AUTH_INIT_PATCHES['logged-out-init'] },
  { id: 'logged-in-init', label: '已登录 · init', patch: AUTH_INIT_PATCHES['logged-in-init'] },
];

export function authInitState(scenario: DevScenarioState): DevAuthInitState | null {
  return scenario.j1 === 'logged-in-init' || scenario.j1 === 'logged-out-init'
    ? scenario.j1
    : null;
}

export function encodeFixtureId(state: DevScenarioState): string {
  const pairs: string[] = [];
  for (const key of Object.keys(FIXTURE_DEFAULTS) as FixtureScenarioKey[]) {
    if (state[key] !== FIXTURE_DEFAULTS[key]) {
      pairs.push(`${key}=${state[key]}`);
    }
  }
  return pairs.length === 0 ? 'default' : pairs.join(';');
}

function mirrorFixtureIdToUrl(state: DevScenarioState): void {
  if (typeof window === 'undefined' || !window.history) {
    return;
  }
  const url = new URL(window.location.href);
  url.searchParams.set(PARITY_SCENARIO_PARAM, authInitState(state) ?? encodeFixtureId(state));
  url.searchParams.delete(LEGACY_FIXTURE_PARAM);
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
}

function scenarioStateFromHash(): DevScenarioState {
  return {
    ...AUTH_INIT_PATCHES['logged-out-init'],
    orgData: 'off',
    j4: 'ready',
    j10: 'ready',
  };
}

function createDevPreviewStore(): DevPreviewStore {
  const initial = scenarioStateFromHash();
  let scenario = initial;
  const initialSession = () => applyJ4Scenario(
    applyJ10Scenario(createInitialSession(), initial.j10),
    initial.j4,
  );
  let session = initialSession();
  const scenarioListeners = new Set<() => void>();
  const sessionListeners = new Set<() => void>();
  return {
    getScenario: () => scenario,
    setScenario: (patch) => {
      scenario = { ...scenario, ...patch };
      scenarioListeners.forEach((listener) => listener());
    },
    subscribeScenario: (listener) => {
      scenarioListeners.add(listener);
      return () => {
        scenarioListeners.delete(listener);
      };
    },
    getSession: () => session,
    dispatchSession: (action) => {
      session = sessionReducer(session, action);
      sessionListeners.forEach((listener) => listener());
    },
    resetSession: () => {
      session = initialSession();
      sessionListeners.forEach((listener) => listener());
    },
    subscribeSession: (listener) => {
      sessionListeners.add(listener);
      return () => {
        sessionListeners.delete(listener);
      };
    },
  };
}

let store: DevPreviewStore | null = null;

export function getDevPreviewStore(): DevPreviewStore {
  store ??= createDevPreviewStore();
  return store;
}

export function resetDevPreviewStore(): void {
  store = null;
}

export function applyFixtureScenario(patch: Partial<DevScenarioState>): void {
  const devStore = getDevPreviewStore();
  devStore.setScenario(patch);
  devStore.resetSession();
  if (patch.j1 === 'logged-in-init') {
    devStore.dispatchSession({ type: 'first-run-complete' });
  }
  mirrorFixtureIdToUrl(devStore.getScenario());
}
