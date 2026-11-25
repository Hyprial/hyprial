/**
 * Decision record 2026-11-23 (平台绑定, I6-4).
 *
 * - Adapter 绑定是 Agent cell 文档的 adapter 成员：读取经 adapter.list（其 agent
 *   字段由 cell 路由索引解析），写入经 agent.cell.get + agent.cell.update（CAS），
 *   不再调用任何 adapter.pin/unpin 方法（已退役）。
 * - 绑定写入是「给 cell 增加一个 role=adapter 成员并在 cell.members 参数里指向本机
 *   adapter 配置」；新成员 ref 与 adapter 同名。解绑与幂等判断按参数里的 adapter
 *   字段匹配（`hyprial agent cell apply` 写入的成员 ref 可以与 adapter 不同名），
 *   覆盖所有 adapter 成员；解绑找不到匹配成员时抛错，不静默返回。
 * - CAS 冲突（INVALID_ARGUMENT 携带 expected/current）与目录拒绝（methodRefusal）
 *   原样抛出，页面据此发通知；只有携带 expected/current 的 INVALID_ARGUMENT 才是
 *   「绑定已变化」，其余结构/参数错误按错误码提示。这里不复制目录的 reason。
 * - 主动外发路由复用独立 adapter.routes.* 请求，绑定与路由不是同一个写操作。
 */

import { assertMethodAvailable, type MethodAccess } from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';
import { createAdapterRoutesApi, type AdapterRoutesApi } from '../routes/api';

export interface AgentAdapterBindingRow {
  readonly name: string;
  readonly provider: string;
  readonly online: boolean;
  readonly processRunning: boolean;
  readonly error?: string;
  readonly boundAgent: string | null;
}

export interface AgentAdapterRouteState {
  readonly available: boolean;
  readonly api?: AdapterRoutesApi;
}

export interface AgentAdapterBindingsApi {
  list(): Promise<readonly AgentAdapterBindingRow[]>;
  bind(adapter: string, agent: string): Promise<void>;
  unbind(adapter: string, agent: string): Promise<void>;
  readonly routes: AgentAdapterRouteState;
}

interface CellMemberEntry {
  readonly ref: string;
  readonly role: string;
}

type CellParams = Record<string, string | number | boolean | unknown[] | Record<string, unknown> | null>;

function adapterRow(value: unknown): AgentAdapterBindingRow {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('平台绑定状态格式不正确。');
  }
  const row = value as Record<string, unknown>;
  if (typeof row.name !== 'string' || !row.name
    || typeof row.provider !== 'string' || !row.provider
    || typeof row.online !== 'boolean'
    || typeof row.processRunning !== 'boolean'
    || (row.agent !== undefined && row.agent !== null && typeof row.agent !== 'string')) {
    throw new Error('平台绑定状态格式不正确。');
  }
  return {
    name: row.name,
    provider: row.provider,
    online: row.online,
    processRunning: row.processRunning,
    error: typeof row.error === 'string' ? row.error : undefined,
    boundAgent: typeof row.agent === 'string' ? row.agent : null,
  };
}

function memberData(params: CellParams): Record<string, unknown> {
  const value = params['cell.members'];
  return value && typeof value === 'object' && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
}

function adapterMembers(
  members: readonly CellMemberEntry[],
  data: Record<string, unknown>,
  adapter: string,
): readonly CellMemberEntry[] {
  return members.filter((member) => {
    if (member.role !== 'adapter') return false;
    const entry = data[member.ref];
    return entry && typeof entry === 'object' && !Array.isArray(entry)
      && (entry as Record<string, unknown>).adapter === adapter;
  });
}

async function updateCell(
  client: Pick<GeneratedClient, 'agent'>,
  agent: string,
  expectedRevision: number,
  members: readonly CellMemberEntry[],
  params: CellParams,
): Promise<void> {
  const response = await client.agent.cell.update({
    name: agent,
    expectedRevision,
    members: members.map(member => ({ ref: member.ref, role: member.role })),
    params,
  });
  if (response?.ok !== true || typeof response.revision !== 'number' || response.revision <= expectedRevision) {
    throw new Error('绑定结果尚未确认。');
  }
}

export function createAgentAdapterBindingsFromClient(
  client: Pick<GeneratedClient, 'adapter' | 'agent'>,
  access?: MethodAccess,
): AgentAdapterBindingsApi {
  async function readCell(agent: string): Promise<{ revision: number; members: readonly CellMemberEntry[]; params: CellParams }> {
    assertMethodAvailable(access, 'agent.cell.get', { name: agent });
    const response = await client.agent.cell.get({ name: agent });
    const cell = response?.cell;
    if (response?.ok !== true || !cell || typeof cell.revision !== 'number' || !Array.isArray(cell.members)
      || !cell.params || typeof cell.params !== 'object' || Array.isArray(cell.params)) {
      throw new Error('Agent cell 读取尚未确认。');
    }
    return { revision: cell.revision, members: cell.members, params: cell.params };
  }

  return {
    routes: { get available() { return access?.('adapter.routes.list')?.available !== false; }, api: createAdapterRoutesApi(client, access) },
    async list() {
      assertMethodAvailable(access, 'adapter.list', {});
      const response = await client.adapter.list({});
      if (!response || response.ok !== true || !Array.isArray(response.adapters)) {
        throw new Error('平台绑定列表尚未确认。');
      }
      return response.adapters.map(adapterRow);
    },
    async bind(adapter, agent) {
      const cell = await readCell(agent);
      const data = memberData(cell.params);
      if (adapterMembers(cell.members, data, adapter).length > 0) return;
      if (cell.members.some(member => member.ref === adapter)) {
        throw new Error(`cell 成员 ${adapter} 已存在，无法作为 adapter 绑定。`);
      }
      assertMethodAvailable(access, 'agent.cell.update', { name: agent, expectedRevision: cell.revision });
      data[adapter] = { adapter };
      await updateCell(
        client, agent, cell.revision,
        [...cell.members, { ref: adapter, role: 'adapter' }],
        { ...cell.params, 'cell.members': data },
      );
    },
    async unbind(adapter, agent) {
      const cell = await readCell(agent);
      const data = memberData(cell.params);
      const refs = new Set(adapterMembers(cell.members, data, adapter).map(member => member.ref));
      if (refs.size === 0) {
        throw new Error(`Agent cell 中没有绑定 ${adapter} 的 adapter 成员。`);
      }
      for (const ref of refs) delete data[ref];
      assertMethodAvailable(access, 'agent.cell.update', { name: agent, expectedRevision: cell.revision });
      await updateCell(
        client, agent, cell.revision,
        cell.members.filter(member => !refs.has(member.ref)),
        { ...cell.params, 'cell.members': data },
      );
    },
  };
}
