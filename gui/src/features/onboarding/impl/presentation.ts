/**
 * Decision record (WP4 data-driven pages, 2026-11-09; synced onto the GUI
 * deep-module layout 2026-11-10):
- Presentations are pure functions of attempt results. The former build-mode
  `DEV && scenario === 'demo'` copy gates (scenario value now `isolated`) are gone; isolated
  previews distinguish themselves through the injected `FirstRunSourceMetadata`
  notice, the demo actions' own result messages and the diagnostic label, so
  identical inputs render identical DOM in dev and production.
- '（回调确认）' remains accurate for isolated previews: their results really
  are returned by local callbacks, and the isolation banner names the scope.
- `_scenario` on account/device presentations is a call-site compatibility
  seam for out-of-scope readers (SettingsPage, AccountLoginSection); it is
  ignored and compositions may stop passing it.
 */
import type { UiTone } from '../../../components/primitives';
import type {
  AccountActionResult,
  AttemptState,
  ChatStatus,
  DeviceActionResult,
  FirstRunScenario,
} from './types';

export interface StatusPresentation {
  readonly value: string;
  readonly tone: UiTone;
  readonly hint: string;
}

function confirmed(): string {
  return '（回调确认）';
}

export function accountPresentation(
  attempt: AttemptState<AccountActionResult>,
  _scenario?: FirstRunScenario,
): StatusPresentation {
  if (attempt.kind === 'running') {
    return {
      value: '正在确认账号',
      tone: 'info',
      hint: '确认前不视为已登录。',
    };
  }

  if (attempt.kind === 'idle') {
    return {
      value: '登录状态待确认',
      tone: 'neutral',
      hint: '点击登录，在授权页确认账号。',
    };
  }

  switch (attempt.result.status) {
    case 'unauthenticated':
      return { value: '尚未登录', tone: 'neutral', hint: attempt.result.message ?? '在授权页完成登录。' };
    case 'authenticated':
      return {
        value: '账号已登录',
        tone: 'neutral',
        hint: attempt.result.message ?? '已确认账号登录。',
      };
    case 'failed':
      return {
        value: '登录失败',
        tone: 'danger',
        hint: '登录失败，可重试。',
      };
    case 'unknown':
      return {
        value: '暂未取得登录结果',
        tone: 'neutral',
        hint: attempt.result.message ?? '请刷新账号状态。',
      };
    case 'unavailable':
      return {
        value: '账号服务不可用',
        tone: 'neutral',
        hint: '账号服务不可用，可重试。',
      };
  }
}

export function devicePresentation(
  attempt: AttemptState<DeviceActionResult>,
  accountAuthenticated: boolean,
  _scenario?: FirstRunScenario,
): StatusPresentation {
  if (attempt.kind === 'running') {
    return {
      value: '正在初始化设备',
      tone: 'info',
      hint: '确认前不视为设备就绪。',
    };
  }

  if (attempt.kind === 'idle') {
    if (!accountAuthenticated) {
      return {
        value: '等待账号确认',
        tone: 'neutral',
        hint: '请先登录账号。',
      };
    }
    return {
      value: '尚未初始化',
      tone: 'neutral',
      hint: '可以初始化当前设备。',
    };
  }

  switch (attempt.result.status) {
    case 'not-ready':
      return { value: '尚未初始化', tone: 'neutral', hint: attempt.result.message ?? '登录后可初始化当前设备。' };
    case 'ready':
      return {
        value: '设备已就绪',
        tone: 'neutral',
        hint: attempt.result.message ?? '已确认设备初始化。',
      };
    case 'failed':
      return {
        value: '设备初始化未完成',
        tone: 'danger',
        hint: '设备未就绪，暂不能继续。',
      };
    case 'unknown':
      return {
        value: '状态未知 · 未初始化',
        tone: 'warning',
        hint: '结果未确认，不视为已初始化。',
      };
    case 'unavailable':
      return {
        value: '不可用 · 未初始化',
        tone: 'warning',
        hint: '设备初始化不可用，可重试。',
      };
  }
}

/** Login and device readiness are not evidence that the network is reachable. */
export function networkPresentation(): StatusPresentation {
  return {
    value: '未检测 · 待检测',
    tone: 'neutral',
    hint: '尚未检测。',
  };
}

export function conversationPresentation(
  status: Exclude<ChatStatus, 'unavailable'>,
): StatusPresentation {
  switch (status) {
    case 'completed':
      return {
        value: `已完成${confirmed()}`,
        tone: 'positive',
        hint: '已确认。',
      };
    case 'ready':
      return {
        value: '可进入 · 未开始',
        tone: 'info',
        hint: '打开聊天，发送第一条消息。',
      };
  }
}
