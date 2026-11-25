import type { Dispatch } from 'react';
import type { AccountSnapshot, FirstRunActions } from '../features/onboarding';
import type { AppAction, FirstRunMode, FirstRunStep } from '../state/types';
import type { GuiCapabilities } from '../app/capabilities';
import type { OrgDomainSource } from '../features/org';
import {
  syntheticProviderLoginFiles,
  type AgentProviderScenario,
} from './mock/agent-provider';

export type AccountLoginScenario = 'success' | 'empty' | 'error' | 'disabled' | 'ready';

export interface FixtureCapabilitiesInput {
  readonly firstRunMode: FirstRunMode;
  /** Scenario data: whether the first-run page is reachable at all in this composition. */
  readonly firstRunWizardEnabled: boolean;
  readonly accountLoginScenario?: AccountLoginScenario;
  readonly agentProviderScenario?: AgentProviderScenario;
  readonly orgSource?: OrgDomainSource;
}

/** The composition shape App consumes through `fixtureCapabilitiesFactory`. */
export type FixtureCapabilitiesFactory = (dispatch: Dispatch<AppAction>) => GuiCapabilities;

/**
 * Data-only capability adapter for fixture compositions. Every entry,
 * including the wizard enablement, comes from the injected input; nothing is
 * derived from build mode or connection state. The returned factory closes
 * over the scenario data and binds to the composition's action dispatch.
 */
export function createFixtureCapabilities(input: FixtureCapabilitiesInput): FixtureCapabilitiesFactory {
  return (dispatch) => Object.freeze({
    firstRun: createFixtureFirstRunActions(input.firstRunMode, dispatch, input.accountLoginScenario),
    firstRunWizardEnabled: input.firstRunWizardEnabled,
    ...(input.agentProviderScenario
      ? {
          agentProviderLoginFiles: async () => syntheticProviderLoginFiles(),
        }
      : {}),
    ...(input.orgSource ? { orgSource: input.orgSource, orgSourceKind: 'isolated' as const } : {}),
  });
}

const DISABLED_LOGIN_REFUSAL = { code: 'METHOD_NOT_FOUND', source: 'auth.login.begin' } as const;

/** Data-only simulation. Never imports a host client or persists input secrets. */
export function createFixtureFirstRunActions(mode: FirstRunMode, dispatch: Dispatch<AppAction>, accountScenario?: AccountLoginScenario): FirstRunActions {
  let generation = 0;
  let account = accountScenario === 'success' || accountScenario === 'ready';
  let device = accountScenario === 'ready';
  let initialized = device;
  const snapshot = (): AccountSnapshot => ({
    account: accountScenario === 'error'
      ? { status: 'failed', message: '模拟：账号授权失败。', retryable: true }
      : account ? { status: 'authenticated', owner: 'preview@example.invalid' } : { status: 'unauthenticated' },
    device: account && device && initialized ? { status: 'ready' } : { status: 'not-ready' },
  });
  const settle = async () => {
    const attempt = generation;
    await new Promise<void>(resolve => setTimeout(resolve, 60));
    if (attempt !== generation) throw new Error('Preview operation cancelled');
  };
  const succeed = (step: FirstRunStep) => {
    dispatch({ type: 'first-run-submit', step });
    dispatch({ type: 'first-run-succeed', step });
  };
  return {
    getAccountState: async () => snapshot(),
    loginRefusal: () => accountScenario === 'disabled' ? DISABLED_LOGIN_REFUSAL : null,
    async authenticateAccount() {
      if (accountScenario === 'disabled') return { status: 'unavailable', refusal: DISABLED_LOGIN_REFUSAL };
      await settle();
      if (mode === 'unavailable') return { status: 'unavailable', message: '模拟：账号服务无响应。' };
      if (mode === 'account-failure') return { status: 'failed', message: '模拟：账号授权失败，请重试。', retryable: true };
      account = true;
      return snapshot().account;
    },
    async logoutAccount() {
      await settle();
      if (!account) return { status: 'logged-out', message: '模拟：当前没有本机登录。', snapshot: snapshot() };
      account = false;
      generation += 1;
      return { status: 'logged-out', message: '模拟：已退出本机登录。', snapshot: snapshot() };
    },
    async initializeDevice() {
      await settle();
      if (!account) return { status: 'not-ready' };
      device = true;
      await settle();
      if (!account) return { status: 'not-ready' };
      initialized = true;
      succeed('account');
      return snapshot().device;
    },
    async cancelAccount() { generation += 1; return { ok: true, cancelled: true }; },
    async saveModelKey() {
      await settle();
      if (mode === 'model-failure') return { status: 'failed', message: '模拟：凭证保存失败，请重试。' };
      if (mode === 'model-unavailable') return { status: 'unavailable', message: '模拟：模型服务无响应。' };
      dispatch({ type: 'first-run-select-provider', provider: 'service-a' });
      dispatch({ type: 'first-run-select-model-method', method: 'api-key' });
      succeed('model');
      return { status: 'saved', message: '模拟：没有实际保存凭证。' };
    },
  };
}
