import type { SetupClient } from '../../platform/setup';
import type { AuthClient, AuthState, AuthProgress } from '../../platform/auth';
import { createUnavailableActions } from './actions';
import type { AccountSnapshot, AccountActionResult, FirstRunActions } from './types';

function accountAndDeviceReady(state: AuthState): boolean {
  return state.authenticated && state.deviceReady && state.sidecarError === null && state.ready;
}

function snapshot(state: AuthState): AccountSnapshot {
  const ready = accountAndDeviceReady(state);
  return {
    account: state.authenticated
      ? {
          status: 'authenticated',
          // The native host may withhold owner until the device is ready.
          owner: state.owner ?? undefined,
          message: ready
            ? '账号与当前设备已就绪。'
            : '账号已登录；当前设备尚未就绪。',
        }
      : { status: 'unauthenticated' },
    device: ready
      ? { status: 'ready', message: '本地设备身份已初始化，账号与设备均已就绪。' }
      : state.authenticated && state.sidecarError
        ? { status: 'failed', message: `设备配置未完成：${state.sidecarError}。请重试设备初始化。`, retryable: true }
        : {
            status: 'not-ready',
            message: state.authenticated
              ? '账号已登录，但当前设备尚未就绪。'
              : undefined,
          },
  };
}

/** Account/device and opt-in Pi setup; model verification requires a real reply. */
export function createAccountActions(client: AuthClient, setup?: SetupClient): FirstRunActions {
  const listeners = new Set<(progress: AuthProgress) => void>();
  const notify = (progress: AuthProgress) => { for (const listener of listeners) listener(progress); };
  return {
    ...createUnavailableActions(),
    ...(setup ? {
      async getSetupState() {
        const result = await setup.status();
        if (!result.ok) throw new Error('SETUP_STATUS_UNAVAILABLE');
        return { keySaved: result.keySaved === true ? true : result.keySaved === false ? false : null,
          created: result.created === true, granted: result.granted === true, grantRecorded: result.grantRecorded === true, started: result.started === true,
          compatible: result.compatible === true, resumeAvailable: result.resumeAvailable === true,
          pendingAction: result.pendingAction ?? null };
      },
      async saveModelKey(key: string) {
        try {
          const result = await setup.saveModelKey(key);
          return result.ok && result.saved
            ? { status: 'saved' as const, message: '凭证已保存，尚未验证模型响应。重新打开页面时会读取当前实例的保存记录。' }
            : { status: 'failed' as const, message: setupMessage(result.code) };
        } catch (error) { return { status: 'unavailable' as const, message: setupMessage(errorCode(error)) }; }
      },
      async prepareSquire(onProgress?: (event: { stage: 'created' | 'granted' | 'started' }) => void) {
        try {
          const result = await setup.prepareSquire(onProgress);
          return { status: result.ok && result.started ? 'started' as const : 'failed' as const,
            created: result.created === true, granted: result.granted === true, grantRecorded: result.grantRecorded === true, started: result.started === true,
            message: result.ok ? 'Pi 已请求启动，模型与会话需收到真实回复后确认。' : setupMessage(result.code) };
        } catch (error) {
          const code = errorCode(error);
          return { status: code === 'SETUP_UNAVAILABLE' || code === 'AUTH_UNAVAILABLE' ? 'unavailable' as const : 'unknown' as const,
            created: false, granted: false, started: false, message: code ? setupMessage(code) : '初始化结果未知，请检查本机状态；不会自动重复修改。' };
        }
      },
      cancelPreparation: () => setup.cancel(),
    } : {}),
    getAccountState: async () => snapshot(await client.status()),
    subscribeAccountProgress: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    cancelAccount: () => client.cancel(),
    async authenticateAccount(): Promise<AccountActionResult> {
      const result = await client.login(notify);
      if (!result.ok) {
        return { status: 'failed', message: '登录未被本机确认，未视为登录成功。请重新打开官方授权页完成授权。', retryable: true };
      }
      let state = result.state;
      if (!state && result.ok) {
        try {
          state = await client.status();
        } catch {
          return { status: 'unknown', message: '登录已返回，但当前账号状态未能确认。请刷新状态后重试。' };
        }
      }
      if (state?.authenticated) return snapshot(state).account;
      return { status: 'failed', message: '登录未完成。请重新打开官方授权页完成授权。', retryable: true };
    },
    async initializeDevice() {
      const result = await client.device(notify);
      if (!result.ok) {
        return { status: 'failed', message: '设备初始化未被本机确认，未视为就绪。请检查本机服务后重试。', retryable: true };
      }
      let state = result.state;
      if (!state && result.ok) {
        try {
          state = await client.status();
        } catch {
          return { status: 'unknown', message: '设备初始化已返回，但当前设备状态未能确认。请刷新状态后重试。' };
        }
      }
      if (state) return snapshot(state).device;
      return { status: 'unknown', message: '未取得设备配置结果，请重新检查状态。' };
    },
  };
}

function errorCode(error: unknown): string | undefined {
  if (error && typeof error === 'object' && 'code' in error && typeof error.code === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(error.code)) return error.code;
  return undefined;
}
function setupMessage(code?: string): string {
  if (code === 'SETUP_UNAVAILABLE' || code === 'AUTH_UNAVAILABLE') return '本机初始化入口未启用。未确认凭证保存或 Squire 启动，请检查本机服务配置。';
  if (code === 'AUTH_NOT_OWNER' || code === 'AUTH_BUSY' || code === 'SETUP_BUSY') return '另一操作正在进行，请在原窗口完成或取消后重试。';
  if (code === 'AUTH_REQUEST_REJECTED') return '本机初始化会话校验未通过，请刷新页面后重新操作。';
  if (code === 'SQUIRE_CONFIGURATION_CONFLICT' || code === 'SQUIRE_EXISTING_REVIEW_REQUIRED' || code === 'SQUIRE_RUNNING_REVIEW_REQUIRED') return '检测到已有 Squire，保留现有配置与凭证。请在设置中确认后继续，不会覆盖或重启。';
  if (code === 'MODEL_KEY_REQUIRED') return '当前会话未确认凭证，请重新输入并保存。';
  if (code === 'MODEL_KEY_ALREADY_GRANTED') return '当前凭证已授权给 Squire。更换凭证请使用设置中的明确变更流程。';
  if (code === 'SETUP_REVIEW_REQUIRED') return '上次操作结果尚未确认，请先检查本机状态，不会自动重复修改。';
  if (code === 'SETUP_CANCELLED') return '初始化已停止；已完成的创建或授权会保留。';
  return '初始化未完成。请检查本机服务与 Pi 环境后重试；未确认模型可用。';
}
