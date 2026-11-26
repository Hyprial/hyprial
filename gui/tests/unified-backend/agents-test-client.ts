/** Test doubles for the generated contract at 92b9f88b1bfc; these are not live backend evidence. */
import { createGeneratedClient } from '../../src/client/generated';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import type { MethodAccess, RequestOptions } from '../../src/client';
import { createAgentsApi } from '../../src/features/agents/impl/api';

export interface AgentsTestPort {
  request(method: string, params: Record<string, unknown>): Promise<unknown>;
}

export function agentsTestClient(
  request: (method: string, params: Record<string, unknown>, options?: RequestOptions) => Promise<unknown>,
) {
  const calls: { method: string; params: Record<string, unknown>; options?: RequestOptions }[] = [];
  const client = createGeneratedClient({
    async invoke<T>(method: string, params: unknown, options?: RequestOptions): Promise<T> {
      calls.push({ method, params: params as Record<string, unknown>, options });
      return await request(method, params as Record<string, unknown>, options) as T;
    },
  });
  return { client, calls };
}

export function createTestAgentsApi(port: AgentsTestPort, access?: MethodAccess) {
  const { client } = agentsTestClient(async (method, params) => {
    const value = await port.request(method, params);
    if (!value || typeof value !== 'object' || ('ok' in value && value.ok !== true)) {
      throw new Error('请求未成功');
    }
    return value;
  });
  const api = createAgentsApi(client, access);
  const { modelStatus: _status, configureModel: _configure, cancelModel: _cancel, nativeTargets: _targets, ...legacy } = api;
  return legacy;
}

export function explicitAgentFixtureSeed() {
  const state = createFixtureState();
  return {
    agents: state.targets.map(agent => ({ ...agent, disabled: agent.availability === 'disabled' })),
    connections: state.connections,
    organizations: [],
  };
}
