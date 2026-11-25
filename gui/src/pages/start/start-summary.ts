/**
 * Start-page status counts.
 *
 * Every card has exactly two columns: normal and abnormal. `null` means the
 * source could not confirm the counts; the card then shows only
 * “状态未确认” and never a half-filled two-column state.
 */
import type { AgentDirectoryRow } from '../../features/agents';

export interface StartStatusCounts {
  readonly normal: number | null;
  readonly abnormal: number | null;
}

export function agentStatusCounts(rows: readonly AgentDirectoryRow[]): StartStatusCounts {
  if (rows.some(row => row.runState.summaryState === undefined)) return { normal: null, abnormal: null };
  const normal = rows.filter(row => row.runState.summaryState === 'normal').length;
  return { normal, abnormal: rows.length - normal };
}

export interface StartSummary {
  readonly agents: StartStatusCounts;
  /** 平台绑定：绑定了平台接入的 Agent；正常=已绑定且转发中，异常=已绑定但不可用。 */
  readonly platformBindings: StartStatusCounts;
}

/** One platform-integration row, reduced to the facts this card counts. */
export interface PlatformBindingFact {
  readonly boundAgent: string | null;
  readonly online: boolean;
  readonly processRunning: boolean;
  readonly error?: string;
}

/** Count pinned platform bindings; an unbound adapter is not a binding. */
export function platformBindingCounts(
  rows: readonly PlatformBindingFact[],
): StartStatusCounts {
  const bound = rows.filter(row => row.boundAgent !== null);
  const normal = bound.filter(row => row.online && row.processRunning && !row.error).length;
  return { normal, abnormal: bound.length - normal };
}
