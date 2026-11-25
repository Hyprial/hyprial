/**
 * implG4 (2026-11-02) 四页签详情的纯投影与文案映射。
 *
 * - 时间标签：同日显示「今天 HH:mm」，否则「MM-DD HH:mm」；now 由调用方注入，
 *   组件传 Date.now()，测试可注入固定值。agent.secret.list 未提供授权/轮换
 *   时间，一律显示「—」，不编造。
 * - 需要注意：只汇总已加载事实中的异常（凭据非 ok），
 *   每条带跳转目标；凭据读取证据未知（loading/error/unknown）时追加
 *   「状态未确认」条目，只有 read 且无异常才显示「暂无需注意」（implG5）。
 * - 2026-11-14：routine/workflow/census 文案投影随从未挂载的 RunTab 删除。
 * - 2026-11-22（G1）：移除 link 文案投影与 linkNotices。
 */

import type { AgentGrantState, AgentProviderGrantView } from '../agent-extensions';
import {
  AGENT_GRANT_STATE_LABELS,
  grantTone,
} from '../agent-extensions';

export type DetailTabId = 'overview' | 'bindings' | 'credentials' | 'relations';

export const DETAIL_TAB_IDS: readonly DetailTabId[] = [
  'overview',
  'bindings',
  'credentials',
  'relations',
];

export const DETAIL_TAB_LABELS: Record<DetailTabId, string> = {
  overview: '概览',
  bindings: '平台绑定',
  credentials: '凭据',
  relations: '所属组织',
};

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/** 同日「今天 HH:mm」，否则「MM-DD HH:mm」。 */
export function formatMsLabel(ms: number, now: number = Date.now()): string {
  const date = new Date(ms);
  const current = new Date(now);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const sameDay =
    date.getFullYear() === current.getFullYear() &&
    date.getMonth() === current.getMonth() &&
    date.getDate() === current.getDate();
  if (sameDay) {
    return `今天 ${time}`;
  }
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

/** 时间缺失时的诚实占位。 */
export const TIME_UNCONFIRMED = '—';

export function grantedTimeLabel(grantedAtLabel: string | undefined): string {
  return grantedAtLabel ?? TIME_UNCONFIRMED;
}

export interface OverviewNotice {
  readonly key: string;
  readonly tone: 'warning' | 'muted' | 'neutral';
  readonly chip: string;
  readonly text: string;
  readonly jumpTo: DetailTabId;
}

function grantNoticeTone(state: AgentGrantState): OverviewNotice['tone'] {
  const tone = grantTone(state);
  return tone === 'positive' ? 'warning' : tone;
}

/** 凭据异常态 → 需要注意条目；ok 记录不进注意区。 */
function credentialNotices(grants: readonly AgentProviderGrantView[]): OverviewNotice[] {
  return grants
    .filter((grant) => grant.state !== 'ok' && grant.state !== 'granted')
    .map((grant) => ({
      key: `grant-${grant.provider}-${grant.state}`,
      tone: grantNoticeTone(grant.state),
      chip: AGENT_GRANT_STATE_LABELS[grant.state],
      text: '凭据授权状态异常',
      jumpTo: 'credentials' as const,
    }));
}

/** 汇总当前已加载事实中的异常；凭据证据未知时必须给状态未确认条目。 */
export function overviewNotices(
  grants: readonly AgentProviderGrantView[],
): OverviewNotice[] {
  return credentialNotices(grants);
}

export const OVERVIEW_NOTICES_EMPTY = '已读取信息中暂无异常。';
