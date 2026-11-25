import { useCallback, useEffect, useRef, useState } from 'react';
import { ClientError, DomainError, HttpError } from '../../../client';
import type { DaemonStatusResult } from './views/contracts';
import type { LocalManagementApi } from './api';

export type LocalManagementState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly status: DaemonStatusResult }
  | { readonly kind: 'failed' | 'unknown' | 'not-ready'; readonly message: string };

function failure(error: unknown): LocalManagementState {
  if ((error instanceof DomainError || error instanceof HttpError)
    && error.dispatch === 'not-dispatched') {
    if (error.code === 'DAEMON_RESTORING') {
      return { kind: 'not-ready', message: '服务正在恢复，暂未执行状态查询。请稍后手动刷新；尚未确认服务就绪。' };
    }
    if (error.code === 'HTTP_NOT_READY') {
      return { kind: 'not-ready', message: '服务状态查询暂未就绪，本次查询未执行。请稍后手动刷新。' };
    }
  }
  if (error instanceof ClientError && error.dispatch === 'unknown') {
    return { kind: 'unknown', message: '查询结果未知，不能判断服务是否正在运行。可手动刷新重新查询。' };
  }
  return { kind: 'failed', message: '未能读取服务状态。请确认本机服务与访问权限后重试；这不表示服务已停止。' };
}

export function useLocalManagement(api: LocalManagementApi) {
  const [snapshot, setSnapshot] = useState<{ api: LocalManagementApi; state: LocalManagementState }>(
    { api, state: { kind: 'loading' } },
  );
  const pending = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSnapshot({ api, state: { kind: 'loading' } });
    try {
      const status = await api.status(controller.signal);
      if (current === generation.current && !controller.signal.aborted) {
        setSnapshot({ api, state: { kind: 'ready', status } });
      }
    } catch (error) {
      if (current === generation.current && !controller.signal.aborted) {
        setSnapshot({ api, state: failure(error) });
      }
    }
  }, [api]);
  useEffect(() => {
    void refresh();
    return () => {
      ++generation.current;
      pending.current?.abort();
    };
  }, [api, refresh]);
  const state: LocalManagementState = snapshot.api === api
    ? snapshot.state
    : { kind: 'loading' };
  return { state, refresh };
}
