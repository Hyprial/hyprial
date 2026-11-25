/** Ephemeral preview route configuration; the shared view uses the production request shape. */
import type { AgentAdapterBindingsApi } from '../../features/agents';
import type { PlatformIntegrationsApi } from '../../features/connections';
type RoutesApi = NonNullable<AgentAdapterBindingsApi['routes']['api']>;
type Snapshot = Awaited<ReturnType<RoutesApi['list']>>;

export function createJourneyRoutes(platforms: PlatformIntegrationsApi) {
  const saved = new Map<string, Snapshot>();
  let revision = 0;
  const nextRevision = () => (++revision).toString(16).padStart(64, '0');
  async function read(name: string): Promise<Snapshot> {
    const row = (await platforms.list()).find(item => item.name === name);
    if (!row) throw new Error('平台接入不存在。');
    let snapshot = saved.get(name);
    if (!snapshot) {
      snapshot = { name, appId: row.authorization.appId ?? `preview-app:${name}`, revision: nextRevision(), defaultRoute: null, routes: [] };
      saved.set(name, snapshot);
    }
    return structuredClone(snapshot);
  }
  async function expected(name: string, revision: string) {
    const snapshot = await read(name);
    if (snapshot.revision !== revision) throw new Error('配置已变化，请刷新路由。');
    return snapshot;
  }
  function write(snapshot: Snapshot) {
    const updated = { ...snapshot, revision: nextRevision() };
    saved.set(snapshot.name, updated);
    return { adapter: structuredClone(updated), configured: true as const, reloaded: true, nextAction: 'none' as const };
  }
  const api: RoutesApi = {
    list: read,
    async candidates(name) {
      await read(name);
      const row = (await platforms.list()).find(item => item.name === name);
      return row?.processRunning ? [{ chatId: 'oc_preview_group', chatType: 'group' }] : [];
    },
    async add(name, route, chatId, makeDefault, revision) {
      const snapshot = await expected(name, revision);
      if (snapshot.routes.some(item => item.name === route)) throw new Error('同名路由已存在。');
      return write({ ...snapshot, defaultRoute: makeDefault ? route : snapshot.defaultRoute,
        routes: [...snapshot.routes, { name: route, type: 'direct', nativeId: chatId }] });
    },
    async remove(name, route, clearDefault, revision) {
      const snapshot = await expected(name, revision);
      if (!snapshot.routes.some(item => item.name === route)) throw new Error('路由不存在。');
      if (snapshot.defaultRoute === route && !clearDefault) throw new Error('请确认清除默认路由。');
      return write({ ...snapshot, routes: snapshot.routes.filter(item => item.name !== route),
        defaultRoute: snapshot.defaultRoute === route ? null : snapshot.defaultRoute });
    },
    reload: () => platforms.reload(),
  };
  return { api, forget: (name: string) => { saved.delete(name); } };
}
