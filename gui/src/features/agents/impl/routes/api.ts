/**
 * Route configuration stays in the daemon; the browser submits one declared request per action.
 * 2026-11-14: a catalog refusal is thrown as MethodRefusedError and never carries a sentence.
 */
import {
  ClientError, DomainError, assertMethodAvailable, type MethodAccess, type MethodAvailability, type MethodRefusal,
} from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';

export interface RouteSnapshot {
  readonly name: string;
  readonly appId: string;
  readonly revision: string;
  readonly defaultRoute: string | null;
  readonly namesStatus?: 'complete' | 'partial' | 'unavailable';
  readonly routes: readonly { readonly name: string; readonly type: 'direct' | 'fanout'; readonly nativeId?: string; readonly members?: readonly string[]; readonly nickname?: string; readonly chatName?: string | null }[];
}
export interface RouteMutation {
  readonly adapter: RouteSnapshot;
  readonly configured: true;
  readonly reloaded: boolean;
  readonly nextAction: 'none' | 'reload';
}
export interface AdapterRoutesApi {
  availability?(action: RouteAction, name: string): MethodAvailability;
  list(name: string): Promise<RouteSnapshot>;
  candidates(name: string): Promise<readonly { readonly chatId: string; readonly chatType: string | null; readonly chatName?: string | null }[]>;
  add(name: string, route: string, chatId: string, makeDefault: boolean, expectedRevision: string): Promise<RouteMutation>;
  remove(name: string, route: string, clearDefault: boolean, expectedRevision: string): Promise<RouteMutation>;
  reload(): Promise<void>;
}

export type RouteAction = 'list' | 'candidates' | 'add' | 'remove' | 'reload';

const ROUTE_METHODS: Readonly<Record<RouteAction, string>> = {
  list: 'adapter.routes.list',
  candidates: 'adapter.routes.candidates',
  add: 'adapter.routes.add',
  remove: 'adapter.routes.remove',
  reload: 'adapter.reload',
};

export function routeRefusal(api: AdapterRoutesApi, action: RouteAction, name: string): MethodRefusal | null {
  const availability = api.availability?.(action, name);
  if (!availability || availability.available) return null;
  return { code: availability.code ?? 'METHOD_NOT_AVAILABLE', source: ROUTE_METHODS[action] };
}

export function routeWriteFailure(error: unknown, unknownMessage: string): { blocked: boolean; message: string } {
  if (error instanceof ClientError && error.code !== 'SUBMIT_OUTCOME_UNKNOWN') {
    if (error.dispatch === 'not-dispatched') return { blocked: false, message: '请求未发送，请重试。' };
    if (error instanceof DomainError && error.dispatch === 'dispatched') {
      const refusals: Record<string, { blocked: boolean; message: string }> = {
        ROUTE_EXISTS: { blocked: false, message: '路由名称已存在。' },
        INVALID_ARGUMENT: { blocked: false, message: '路由配置无效，请检查输入。' },
        CALLER_NOT_AUTHORIZED: { blocked: true, message: '当前会话无权修改路由。' },
        DAEMON_NOT_READY: { blocked: true, message: '本机服务未就绪，请稍后刷新路由。' },
        ADAPTER_CONFIG_CONFLICT: { blocked: true, message: '配置已变化，请刷新路由。' },
        ADAPTER_NOT_FOUND: { blocked: true, message: '平台接入已不存在，请刷新平台绑定。' },
        ROUTE_NOT_FOUND: { blocked: true, message: '路由已不存在，请刷新路由。' },
        ROUTE_IN_USE: { blocked: true, message: '路由仍被引用，请刷新路由。' },
      };
      if (refusals[error.code]) return refusals[error.code];
    }
  }
  return { blocked: true, message: unknownMessage };
}

export function createAdapterRoutesApi(client: Pick<GeneratedClient, 'adapter'>, access?: MethodAccess): AdapterRoutesApi {
  function require(method: string, params: object): void {
    assertMethodAvailable(access, method, params);
  }
  return {
    availability(action, name) {
      return access?.(ROUTE_METHODS[action], action === 'reload' ? {} : { name }) ?? { available: true };
    },
    async list(name) {
      require('adapter.routes.list', { name });
      return (await client.adapter.routes.list({ name })).adapter;
    },
    async candidates(name) {
      require('adapter.routes.candidates', { name });
      return (await client.adapter.routes.candidates({ name })).candidates;
    },
    async add(name, route, chatId, makeDefault, expectedRevision) {
      const params = { name, route, chatId, makeDefault, expectedRevision };
      require('adapter.routes.add', params);
      return client.adapter.routes.add(params);
    },
    async remove(name, route, clearDefault, expectedRevision) {
      const params = { name, route, clearDefault, expectedRevision };
      require('adapter.routes.remove', params);
      return client.adapter.routes.remove(params);
    },
    async reload() {
      require('adapter.reload', {});
      await client.adapter.reload({});
    },
  };
}
