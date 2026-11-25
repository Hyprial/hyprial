import type { Dispatch } from 'react';
import {
  createFixtureFirstRunActions,
  type AccountLoginScenario,
} from '../../src/dev/first-run-capabilities';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import {
  syntheticProviderLoginFiles,
  type AgentProviderScenario,
} from '../../src/dev/mock/agent-provider';
import type { AppAction, AppState, ChatSeed, FirstRunMode } from '../../src/state/types';
import type { GuiCapabilities } from '../../src/app/capabilities';
import type { StateBaseline } from '../../src/state/appReducer';
import { type FirstRunActions } from '../../src/features/onboarding';
import {
  FIXTURE_AGENTS,
  FIXTURE_CONNECTIONS,
  FIXTURE_THREADS,
} from '../../src/dev/fixtures/demo';
import { stubFirstRunActions } from './first-run-actions';

export const DEMO_BASELINE: StateBaseline = {
  targets: FIXTURE_AGENTS,
  connections: FIXTURE_CONNECTIONS,
  threads: FIXTURE_THREADS,
};

export function authenticatedTestActions(): FirstRunActions {
  return {
    ...stubFirstRunActions(),
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'test@example.invalid' },
      device: { status: 'ready' },
    }),
  };
}

export const AUTHENTICATED_TEST_CAPABILITIES: GuiCapabilities = { firstRun: authenticatedTestActions() };

export function demoFixtureState(
  firstRunMode: FirstRunMode = 'complete',
  chatSeed: ChatSeed = {},
): AppState {
  return createFixtureState(firstRunMode, null, chatSeed);
}

/**
 * Assembles the fixture capability set for compositions under test. The
 * dispatch comes from the app itself (`mockCapabilitiesFactory`), so the
 * simulated actions land in the same reducer the product renders from.
 */
export function demoFixtureCapabilities(
  dispatch: Dispatch<AppAction>,
  input: {
    readonly firstRunMode?: Parameters<typeof createFixtureFirstRunActions>[0];
    readonly firstRunWizardEnabled?: boolean;
    readonly accountLoginScenario?: AccountLoginScenario;
    readonly agentProviderScenario?: AgentProviderScenario;
  } = {},
): GuiCapabilities {
  return Object.freeze({
    firstRun: createFixtureFirstRunActions(
      input.firstRunMode ?? 'complete',
      dispatch,
      input.accountLoginScenario ?? ((input.firstRunMode ?? 'complete') === 'complete' ? 'ready' : undefined),
    ),
    firstRunWizardEnabled: input.firstRunWizardEnabled ?? true,
    ...(input.agentProviderScenario
      ? {
          agentProviderLoginFiles: async () => syntheticProviderLoginFiles(),
        }
      : {}),
  });
}
