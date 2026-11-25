import type { AgentAdapterBindingRow, AgentAdapterBindingsApi } from '../../../src/features/agents';

export interface MockAgentAdapterBindingsSeed {
  readonly adapters?: readonly AgentAdapterBindingRow[];
}

export function createMockAgentAdapterBindings(
  seed: MockAgentAdapterBindingsSeed = {},
): AgentAdapterBindingsApi {
  let rows: AgentAdapterBindingRow[] = [...(seed.adapters ?? [
    {
      name: 'feishu-main',
      provider: 'lark',
      online: true,
      processRunning: true,
      boundAgent: null,
    },
    {
      name: 'feishu-ops',
      provider: 'lark',
      online: false,
      processRunning: false,
      error: 'adapter 未启动',
      boundAgent: 'agent-researcher',
    },
  ])];

  function find(name: string): AgentAdapterBindingRow {
    const row = rows.find(value => value.name === name);
    if (!row) throw new Error('平台绑定不存在。');
    return row;
  }

  return {
    routes: { available: false },
    async list() {
      return rows.map(row => ({ ...row }));
    },
    async bind(adapter, agent) {
      find(adapter);
      rows = rows.map(value => value.name === adapter ? { ...value, boundAgent: agent } : value);
    },
    async unbind(adapter, agent) {
      const row = find(adapter);
      if (row.boundAgent !== agent) {
        throw new Error('绑定已变化，请刷新后重新确认。');
      }
      rows = rows.map(value => value.name === adapter ? { ...value, boundAgent: null } : value);
    },
  };
}
