/**
 * implG1 (2026-11-02) 决策记录：
 * - 目录读取的生命周期 hook：unwired/loading/ready/error 四态，错误不落空列表，
 *   重试只重读（agent.list retry=safe）。组件不持有传输细节。
 * - 详情打开时按 actor 读取一次 Key 授权（agent.secret.list），概览的
 *   “状态未确认”提示与凭据页签共用同一读取结果；Auth 没有读取方法。
 * - 2026-11-14：Key 授权读取在发出前被拒绝（methodRefusal）视为没有这个读取端口，
 *   视图状态 unavailable 不带任何文案，页面不渲染该块。
 * - 2026-11-14（验证修订）：api 成员不存在（injected client 缺 handler）等同没有端口：
 *   目录为 unwired，Key 授权为 unavailable。
 * - 2026-11-22（G1）：移除 useAgentLinks 与 link.list 读取 hook。
 * - 2026-11-14：运行页签从未挂载，随 RunTab 删除了 routine.list /
 *   workflow.list / agent.home-census 的读取 hook 与其文案投影；对应
 *   AgentsApi 方法保留为端口，待有真实页面再接。
 * - 前台目录每两秒独立读取，失败保留上次事实；切换来源及卸载丢弃旧回包。
 * - Key 读取错误属于凭据区；概览只汇总已知授权异常，不推断 Auth 故障。
 */

import { useEffect, useState } from 'react';

import { methodRefusal } from '../../../../client';

import type {
  AgentDirectoryRow,
  AgentExpected,
  AgentProviderGrant,
  AgentsApi,
} from './index';

export type DirectoryStatus = 'unwired' | 'loading' | 'ready' | 'error';

export interface AgentDirectoryState {
  readonly status: DirectoryStatus;
  readonly rows: readonly AgentDirectoryRow[];
  readonly error?: string;
  reload(): void;
}

export function useAgentDirectory(api?: AgentsApi): AgentDirectoryState {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<AgentDirectoryState & { source?: AgentsApi }>({
    status: 'unwired',
    rows: [],
    reload: () => setAttempt((value) => value + 1),
  });

  const reload = state.reload;

  useEffect(() => {
    const list = api?.list?.bind(api);
    if (!api || !list) {
      setState({ status: 'unwired', rows: [], reload });
      return;
    }
    let cancelled = false;
    let reading = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState((current) => ({
      ...current,
      status: 'loading',
      source: api,
      rows: current.source === api ? current.rows : [],
      error: undefined,
    }));
    const schedule = () => {
      clearTimeout(timer);
      if (!cancelled && !reading && document.visibilityState !== 'hidden') timer = setTimeout(() => void read(), 2000);
    };
    const read = async () => {
      if (cancelled || reading) return;
      reading = true;
      try {
        const rows = await list();
        if (!cancelled) setState({ status: 'ready', rows, reload, source: api });
      } catch (error) {
        if (!cancelled) setState(current => ({
          ...current, status: current.status === 'ready' ? 'ready' : 'error',
          error: error instanceof Error ? error.message : String(error),
        }));
      } finally {
        reading = false;
        schedule();
      }
    };
    void read();
    document.addEventListener('visibilitychange', schedule);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  }, [api, attempt, reload]);

  return state.source === api ? state : { status: api ? 'loading' : 'unwired', rows: [], reload };
}

export type AgentProviderStatusView =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly rows: readonly AgentProviderGrant[] }
  | { readonly status: 'error'; readonly error: string }
  | { readonly status: 'unavailable' };

export interface AgentProviderStatusState {
  readonly view: AgentProviderStatusView;
  reload(): void;
}

/**
 * 账号 Key 授权记录（agent.secret.list），只在凭据页签激活时读取。
 * 读取失败不落空成功：页面显示“状态未确认”，不显示“没有授权”。
 */
export function useAgentProviderStatus(
  api: AgentsApi | undefined,
  actor: string | null,
  enabled: boolean,
  expectedAgent?: AgentExpected | null,
): AgentProviderStatusState {
  const [attempt, setAttempt] = useState(0);
  const [view, setView] = useState<AgentProviderStatusView>({ status: 'idle' });
  const expectedUri = expectedAgent?.uri ?? null;
  const expectedCreatedAtMs = expectedAgent?.createdAtMs ?? null;

  useEffect(() => {
    if (!enabled || !api || !actor) {
      setView({ status: 'idle' });
      return;
    }
    const readStatus = api.providerStatus?.bind(api);
    if (!readStatus) {
      setView({ status: 'unavailable' });
      return;
    }
    let cancelled = false;
    setView({ status: 'loading' });
    readStatus(actor, expectedUri && expectedCreatedAtMs !== null
        ? { uri: expectedUri, createdAtMs: expectedCreatedAtMs }
        : null)
      .then((rows) => {
        if (!cancelled) setView({ status: 'ready', rows });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setView(
            methodRefusal(error) !== null
              ? { status: 'unavailable' }
              : { status: 'error', error: error instanceof Error ? error.message : String(error) },
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [actor, api, attempt, enabled, expectedCreatedAtMs, expectedUri]);

  return { view, reload: () => setAttempt((value) => value + 1) };
}
