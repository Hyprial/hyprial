/**
 * Decision record (GUI deep modules C2, 2026-11-08): the isolated demo
 * first-run callbacks moved out of the formal onboarding feature. Development
 * compositions and tests inject them explicitly; the formal controller
 * has no fallback action set.
 */
import type {
  AccountActionResult,
  DeviceActionResult,
  FirstRunActions,
} from '../features/onboarding';

/**
 * Isolated demonstration callbacks. Successful states exist only to exercise
 * the private preview; they never contact a service or create product state.
 * The UI renders a prominent isolated-preview notice and `data-scenario="demo"`.
 */
export function createDemoActions(): FirstRunActions {
  return {
    async saveModelKey() { return { status: 'saved', message: '隔离预览：没有实际保存凭证。' }; },
    async authenticateAccount(): Promise<AccountActionResult> {
      return {
        status: 'authenticated',
        message: '隔离预览：账号确认仅由本地隔离回调返回。',
      };
    },
    async initializeDevice(): Promise<DeviceActionResult> {
      return {
        status: 'ready',
        message: '隔离预览：设备就绪状态仅由本地隔离回调返回。',
      };
    },
  };
}
