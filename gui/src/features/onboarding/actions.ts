import type { ModelValidationResult } from '../model';
import type {
  AccountActionResult,
  DeviceActionResult,
  FirstRunActions,
  SquireActionResult,
} from './types';

const UNAVAILABLE_ACCOUNT =
  '账号服务当前不可用。未执行登录或注册；本次输入不会离开页面。';
const UNAVAILABLE_DEVICE =
  '设备初始化当前不可用。未生成设备身份，也未写入本地存储。';
const UNAVAILABLE_MODEL =
  '模型服务当前不可用。未发起校验，状态保持未知。';
const UNAVAILABLE_SQUIRE_CREATE =
  'Squire 创建当前不可用。未创建任何进程或记录。';
const UNAVAILABLE_SQUIRE_START =
  'Squire 启动当前不可用。未启动任何进程。';

/**
 * The formal no-backend path. Every callback resolves locally with an
 * unavailable/unknown result; no adapter, request or storage access occurs.
 */
export function createUnavailableActions(): FirstRunActions {
  return {
    async saveModelKey() { return { status: 'unavailable', message: '凭证保存入口尚未接入。' }; },
    async prepareSquire() { return { status: 'unavailable', created: false, granted: false, started: false, message: 'Squire 准备入口尚未接入。' }; },
    async authenticateAccount(): Promise<AccountActionResult> {
      return { status: 'unavailable', message: UNAVAILABLE_ACCOUNT };
    },
    async initializeDevice(): Promise<DeviceActionResult> {
      return { status: 'unavailable', message: UNAVAILABLE_DEVICE };
    },
    async validateModel(): Promise<ModelValidationResult> {
      return { status: 'unknown', message: UNAVAILABLE_MODEL };
    },
    async createSquire(): Promise<SquireActionResult> {
      return { status: 'unavailable', message: UNAVAILABLE_SQUIRE_CREATE };
    },
    async startSquire(): Promise<SquireActionResult> {
      return { status: 'unavailable', message: UNAVAILABLE_SQUIRE_START };
    },
  };
}

/**
 * Isolated demonstration callbacks. Successful states exist only to exercise
 * the private preview; they never contact a service or create product state.
 * The UI renders a prominent isolated-preview notice and `data-scenario="demo"`.
 */
export function createDemoActions(): FirstRunActions {
  return {
    async saveModelKey() { return { status: 'saved', message: '隔离预览：没有实际保存凭证。' }; },
    async prepareSquire(onProgress) {
      for (const stage of ['created', 'granted', 'started'] as const) onProgress?.({ stage });
      return { status: 'started', created: true, granted: true, started: true, message: '隔离预览：没有创建或启动真实进程。' };
    },
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
    async validateModel(): Promise<ModelValidationResult> {
      return {
        status: 'connected',
        detail: '隔离预览：连接状态由本地隔离回调确认。',
      };
    },
    async createSquire(): Promise<SquireActionResult> {
      return {
        status: 'created',
        message: '隔离预览：Squire 创建状态由本地回调返回。',
      };
    },
    async startSquire(): Promise<SquireActionResult> {
      return {
        status: 'ready',
        message: '隔离预览：Squire 运行状态由本地回调返回。',
      };
    },
  };
}
