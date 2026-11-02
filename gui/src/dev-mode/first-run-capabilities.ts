import type { Dispatch } from 'react';
import { createUnavailableActions, type AccountSnapshot, type FirstRunActions } from '../features/onboarding';
import type { AppAction, FirstRunMode, FirstRunStep } from '../state/types';

export type AccountLoginScenario = 'success' | 'empty' | 'error' | 'disabled';

/** Data-only simulation. Never imports a host client or persists input secrets. */
export function createFixtureFirstRunActions(mode: FirstRunMode, dispatch: Dispatch<AppAction>, accountScenario?: AccountLoginScenario): FirstRunActions {
  const unavailable = createUnavailableActions();
  let generation = 0;
  let account = accountScenario === 'success';
  let device = false;
  const snapshot = (): AccountSnapshot => ({
    account: accountScenario === 'error'
      ? { status: 'failed', message: '模拟：账号授权失败。', retryable: true }
      : account ? { status: 'authenticated', owner: 'preview@example.invalid' } : { status: 'unauthenticated' },
    device: device ? { status: 'ready' } : { status: 'not-ready' },
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
    ...unavailable,
    getAccountState: async () => snapshot(),
    async authenticateAccount() {
      await settle();
      if (mode === 'unavailable') return { status: 'unavailable', message: '模拟：账号服务不可用。' };
      if (mode === 'account-failure') return { status: 'failed', message: '模拟：账号授权失败，请重试。', retryable: true };
      account = true;
      return snapshot().account;
    },
    async initializeDevice() {
      await settle();
      if (!account) return { status: 'not-ready' };
      device = true;
      succeed('account');
      return snapshot().device;
    },
    async cancelAccount() { generation += 1; return { ok: true, cancelled: true }; },
    async saveModelKey() {
      await settle();
      if (mode === 'model-failure') return { status: 'failed', message: '模拟：凭证保存失败，请重试。' };
      if (mode === 'model-unavailable') return { status: 'unavailable', message: '模拟：模型服务暂不可用。' };
      dispatch({ type: 'first-run-select-provider', provider: 'service-a' });
      dispatch({ type: 'first-run-select-model-method', method: 'api-key' });
      succeed('model');
      return { status: 'saved', message: '模拟：没有实际保存凭证。' };
    },
    async prepareSquire(onProgress) {
      await settle();
      if (mode === 'squire-failure') return { status: 'failed', created: false, granted: false, started: false, message: '模拟：Squire 准备失败，请重试。' };
      for (const stage of ['created', 'granted', 'started'] as const) onProgress?.({ stage });
      succeed('squire');
      return { status: 'started', created: true, granted: true, started: true, message: '模拟：没有创建或启动真实 Squire。' };
    },
    async cancelPreparation() { generation += 1; },
  };
}
