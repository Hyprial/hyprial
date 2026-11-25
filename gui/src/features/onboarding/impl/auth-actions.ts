import { publicErrorCode } from '../../../platform/shared/public-error-code';
import { isSaveNotDispatchedCode, type SetupClient } from '../../../platform/setup';
import type { AuthClient, AuthState, AuthProgress } from '../../../platform/auth';
import { unknownModelKeySaveResult } from './model-save-result';
import type { AccountSnapshot, AccountActionResult, FirstRunActions, ModelKeySaveResult } from './types';

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
      ? { status: 'ready', message: '账号与设备均已就绪。' }
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

function thrownSaveFailure(code?: string): ModelKeySaveResult {
  if (!code || !isSaveNotDispatchedCode(code)) return unknownModelKeySaveResult();
  return code === 'SETUP_UNAVAILABLE' || code === 'AUTH_UNAVAILABLE'
    ? { status: 'unavailable', message: setupMessage(code) }
    : { status: 'failed', message: setupMessage(code) };
}

/** Account/device and opt-in key/status setup; model verification requires a real reply. */
export function createAccountActions(client: AuthClient, setup?: SetupClient): FirstRunActions {
  const listeners = new Set<(progress: AuthProgress) => void>();
  const notify = (progress: AuthProgress) => { for (const listener of listeners) listener(progress); };
  const open = client.open;
  const logout = client.logout;
  return {
    ...(typeof logout === 'function' ? {
      async logoutAccount() {
        try {
          const receipt = await logout.call(client);
          if (receipt.ok !== true || receipt.localOnly !== true
            || (receipt.status !== 'logged-out' && receipt.status !== 'not-logged-in')) {
            throw new Error('AUTH_LOGOUT_UNKNOWN');
          }
          const state = await client.status();
          if (state.authenticated !== false) throw new Error('AUTH_LOGOUT_UNKNOWN');
          return { status: 'logged-out' as const, snapshot: snapshot(state),
            message: receipt.status === 'not-logged-in'
              ? '当前没有用户登录；机器与 Agent 凭据保留。'
              : '已退出本机登录；机器与 Agent 凭据保留，已签发令牌未撤销。' };
        } catch (error) {
          const code = errorCode(error);
          const refused = code === 'AUTH_BUSY' || code === 'AUTH_REQUEST_REJECTED' || code === 'AUTH_UNAVAILABLE'
            || code === 'AUTH_CONFIGURATION_INVALID' || code === 'AUTH_SESSION_LIMIT' || code === 'AUTH_ORIGIN_REJECTED';
          return { status: refused ? 'failed' as const : 'unknown' as const,
            message: refused ? '未能退出，请检查本机服务后重试。'
              : '退出结果未知，请刷新账号状态确认。' };
        }
      },
    } : {}),
    ...(setup ? {
      async getSetupState() {
        const result = await setup.status();
        if (!result.ok) throw new Error('SETUP_STATUS_UNAVAILABLE');
        return {
          keySaved: result.keySaved === true ? true : result.keySaved === false ? false : null,
          pendingAction: result.pendingAction ?? null,
        };
      },
      async saveModelKey(key: string) {
        try {
          const result = await setup.saveModelKey(key);
          const ok: unknown = result.ok;
          if (typeof ok !== 'boolean') return unknownModelKeySaveResult();
          if (!ok) return thrownSaveFailure(result.code);
          const saved: unknown = result.saved;
          if (saved === true) {
            return { status: 'saved' as const, message: '凭证已保存，尚未验证模型响应。' };
          }
          if (saved === false) return { status: 'failed' as const, message: setupMessage(result.code) };
          return unknownModelKeySaveResult();
        } catch (error) { return thrownSaveFailure(errorCode(error)); }
      },
    } : {}),
    getAccountState: async () => snapshot(await client.status()),
    subscribeAccountProgress: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    cancelAccount: () => client.cancel(),
    ...(typeof open === 'function'
      ? { openAuthorization: () => open.call(client) }
      : {}),
    async authenticateAccount(): Promise<AccountActionResult> {
      const result = await client.login(notify);
      if (!result.ok) {
        return { status: 'failed', message: '登录未确认，未视为成功。请重新打开授权页完成授权。', retryable: true };
      }
      let state = result.state;
      if (!state && result.ok) {
        try {
          state = await client.status();
        } catch {
          return { status: 'unknown', message: '账号状态未能确认，请刷新后重试。' };
        }
      }
      if (state?.authenticated) return snapshot(state).account;
      return { status: 'failed', message: '登录未完成，请重新打开授权页完成授权。', retryable: true };
    },
    async initializeDevice() {
      const result = await client.device(notify);
      if (!result.ok) {
        return { status: 'failed', message: '设备初始化未确认，未视为就绪。请检查本机服务后重试。', retryable: true };
      }
      let state = result.state;
      if (!state && result.ok) {
        try {
          state = await client.status();
        } catch {
          return { status: 'unknown', message: '设备状态未能确认，请刷新后重试。' };
        }
      }
      if (state) return snapshot(state).device;
      return { status: 'unknown', message: '未取得设备配置结果，请重新检查。' };
    },
  };
}

function errorCode(error: unknown): string | undefined {
  if (error && typeof error === 'object' && 'code' in error) return publicErrorCode(error.code);
  return undefined;
}
function setupMessage(code?: string): string {
  if (code === 'SETUP_RESULT_UNKNOWN') return '结果未知，请检查本机状态。';
  if (code === 'SETUP_UNAVAILABLE' || code === 'AUTH_UNAVAILABLE') return '本机初始化未启用，未确认凭证保存。';
  if (code === 'AUTH_NOT_OWNER' || code === 'AUTH_BUSY' || code === 'SETUP_BUSY') return '另一操作正在进行，请完成或取消后重试。';
  if (code === 'AUTH_REQUEST_REJECTED') return '会话校验未通过，请刷新页面后重试。';
  if (code === 'MODEL_KEY_REQUIRED') return '未确认凭证，请重新输入并保存。';
  if (code === 'MODEL_KEY_ALREADY_GRANTED') return '已有凭据正在使用当前配置。如需更换，请到设置中操作。';
  if (code === 'SETUP_REVIEW_REQUIRED') return '上次操作结果未确认，请先检查本机状态。';
  return '初始化未完成，请检查本机服务后重试。';
}
