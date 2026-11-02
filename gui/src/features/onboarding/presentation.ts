import type { UiTone } from '../../components/primitives';
import type {
  AccountActionResult,
  AttemptState,
  ChatStatus,
  DeviceActionResult,
  FirstRunScenario,
  SquireActionResult,
} from './types';

export interface StatusPresentation {
  readonly value: string;
  readonly tone: UiTone;
  readonly hint: string;
}

function confirmed(scenario: FirstRunScenario): string {
  return scenario === 'demo' ? '（隔离预览）' : '（回调确认）';
}

function idleScenario(scenario: FirstRunScenario): string {
  return scenario === 'demo' && import.meta.env.DEV ? '隔离预览场景' : '未接线';
}

export function accountPresentation(
  attempt: AttemptState<AccountActionResult>,
  scenario: FirstRunScenario,
): StatusPresentation {
  if (attempt.kind === 'running') {
    return {
      value: '正在确认 · 状态未知',
      tone: 'info',
      hint: '结果返回前不视为已登录。',
    };
  }

  if (attempt.kind === 'idle') {
    return {
      value: scenario === 'demo' && import.meta.env.DEV ? '未登录 · 隔离预览场景' : '登录状态待确认',
      tone: 'neutral',
      hint:
        scenario === 'demo'
          ? '点击登录后会由隔离预览回调确认。'
          : '点击登录，在官方授权页确认账号。',
    };
  }

  switch (attempt.result.status) {
    case 'unauthenticated':
      return { value: '尚未登录', tone: 'neutral', hint: attempt.result.message ?? '在官方授权页完成登录。' };
    case 'authenticated':
      return {
        value: `已确认${confirmed(scenario)} · 账号已登录`,
        tone: 'positive',
        hint: attempt.result.message ?? '当前实例已确认账号登录。',
      };
    case 'failed':
      return {
        value: '登录失败 · 未登录',
        tone: 'danger',
        hint: '失败不会推进步骤；可在当前步骤重试。',
      };
    case 'unknown':
      return {
        value: '状态未知 · 未登录',
        tone: 'warning',
        hint: '未取得可确认的结果；不会显示为已登录。',
      };
    case 'unavailable':
      return {
        value: '不可用 · 未登录',
        tone: 'warning',
        hint: '账号服务未接入；可在当前步骤重试。',
      };
  }
}

export function devicePresentation(
  attempt: AttemptState<DeviceActionResult>,
  accountAuthenticated: boolean,
  scenario: FirstRunScenario,
): StatusPresentation {
  if (attempt.kind === 'running') {
    return {
      value: '初始化中 · 状态未知',
      tone: 'info',
      hint: '结果返回前不视为设备就绪。',
    };
  }

  if (attempt.kind === 'idle') {
    if (!accountAuthenticated) {
      return {
        value: '等待账号确认',
        tone: 'neutral',
        hint: '先完成账号确认，再初始化当前设备。',
      };
    }
    return {
      value: `未初始化 · ${idleScenario(scenario)}`,
      tone: 'neutral',
      hint: '账号已确认，可以初始化当前设备。',
    };
  }

  switch (attempt.result.status) {
    case 'not-ready':
      return { value: '尚未初始化', tone: 'neutral', hint: attempt.result.message ?? '账号登录后，可初始化当前设备。' };
    case 'ready':
      return {
        value: `已就绪${confirmed(scenario)} · 本地设备身份已初始化`,
        tone: 'positive',
        hint: attempt.result.message ?? '当前实例已确认本地设备身份初始化。',
      };
    case 'failed':
      return {
        value: '初始化失败 · 未初始化',
        tone: 'danger',
        hint: '设备未就绪，当前步骤不能继续。',
      };
    case 'unknown':
      return {
        value: '状态未知 · 未初始化',
        tone: 'warning',
        hint: '未取得可确认的结果；不会显示为已初始化。',
      };
    case 'unavailable':
      return {
        value: '不可用 · 未初始化',
        tone: 'warning',
        hint: '设备初始化未接入；可在当前步骤重试。',
      };
  }
}

/** Login and device readiness are not evidence that the network is reachable. */
export function networkPresentation(): StatusPresentation {
  return {
    value: '未检测 · 待检测',
    tone: 'neutral',
    hint: '账号登录与本地设备初始化不等于网络已连接；当前没有独立网络检测证据。',
  };
}

function squireResultPresentation(
  result: SquireActionResult,
  scenario: FirstRunScenario,
  phase: 'create' | 'start',
): StatusPresentation {
  switch (result.status) {
    case 'ready':
      return {
        value: `已启动${confirmed(scenario)}`,
        tone: 'positive',
        hint: result.message ?? 'Squire 状态由调用方回调确认。',
      };
    case 'created':
      if (phase === 'start') {
        return {
          value: '启动结果未知 · 已创建',
          tone: 'warning',
          hint: '启动回调未确认运行状态；不会显示为已启动。',
        };
      }
      return {
        value: '已创建 · 未启动',
        tone: 'info',
        hint: result.message ?? '创建回调已完成，仍需启动。',
      };
    case 'failed':
      return {
        value:
          phase === 'create'
            ? '创建失败 · 未创建'
            : '启动失败 · 未启动',
        tone: 'danger',
        hint: '失败不会推进步骤；可在当前步骤重试。',
      };
    case 'unknown':
      return {
        value:
          phase === 'create'
            ? '状态未知 · 未创建'
            : '状态未知 · 未启动',
        tone: 'warning',
        hint: '未取得可确认的结果；不会显示为运行中。',
      };
    case 'unavailable':
      return {
        value:
          phase === 'create'
            ? '不可用 · 未创建'
            : '不可用 · 未启动',
        tone: 'warning',
        hint: 'Squire 服务未接入；可在当前步骤重试。',
      };
  }
}

export function squirePresentation(
  createAttempt: AttemptState<SquireActionResult>,
  startAttempt: AttemptState<SquireActionResult>,
  scenario: FirstRunScenario,
): StatusPresentation {
  if (startAttempt.kind === 'running') {
    return {
      value: '正在启动 · 状态未知',
      tone: 'info',
      hint: '结果返回前不视为运行中。',
    };
  }
  if (startAttempt.kind === 'settled') {
    return squireResultPresentation(startAttempt.result, scenario, 'start');
  }
  if (createAttempt.kind === 'running') {
    return {
      value: '正在创建 · 状态未知',
      tone: 'info',
      hint: '结果返回前不视为已创建。',
    };
  }
  if (createAttempt.kind === 'settled') {
    return squireResultPresentation(createAttempt.result, scenario, 'create');
  }
  return {
    value: `未创建 · ${idleScenario(scenario)}`,
    tone: 'neutral',
    hint: 'Squire 是必填步骤，本流程不提供跳过入口。',
  };
}

export function conversationPresentation(
  status: ChatStatus,
  scenario: FirstRunScenario,
): StatusPresentation {
  switch (status) {
    case 'completed':
      return {
        value: `已完成${confirmed(scenario)}`,
        tone: 'positive',
        hint: '完成状态必须由跨 track 聊天集成回调确认。',
      };
    case 'ready':
      return {
        value: '可进入 · 未开始',
        tone: 'info',
        hint: '本组件不发送消息；打开聊天后由聊天 track 负责收发。',
      };
    case 'unavailable':
    default:
      return {
        value: '不可用 · 未接入',
        tone: 'warning',
        hint: '聊天能力尚未接入；不会模拟发送或回复。',
      };
  }
}
