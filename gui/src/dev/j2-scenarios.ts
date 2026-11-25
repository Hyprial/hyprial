import {
  buildManagedAgent,
} from '../state/agent-selectors';
import type { AgentSeed } from '../state/agent-selectors';
import {
  createLocalAgentGateway,
  type AgentMutationGateway,
} from '../features/agents';
import { M04_SEED, M05_SEED, M06_SEED } from './fixtures/agents/j2-seeds';

/**
 * J2 development scenarios.
 *
 * Only the four R4 mock scenes are selectable. Create success is a local
 * interaction inside M-04 and M-05; failure and unknown results have no scene
 * number and are injected by tests instead. 种子数据自 implG1 起由开发
 * fixtures 权威提供（C2 起位于 `gui/src/dev/fixtures/agents/j2-seeds.ts`，
 * M-04/M-05/M-06 迁移）。
 */
export const J2_SCENARIOS = [
  { value: 'baseline', label: '基线（无 Agent 数据）' },
  { value: 'm04', label: 'M-04 Agent 列表有数据' },
  { value: 'm05', label: 'M-05 Agent 列表为空' },
  { value: 'm06', label: 'M-06 Agent 需连接模型' },
] as const;

export type J2Scenario = (typeof J2_SCENARIOS)[number]['value'];

export function isJ2Scenario(value: string): value is J2Scenario {
  return J2_SCENARIOS.some((option) => option.value === value);
}

export interface AgentScenario {
  readonly seed: AgentSeed;
  readonly gateway: AgentMutationGateway;
}

export function buildAgentScenario(
  scenario: J2Scenario,
): AgentScenario | undefined {
  if (scenario === 'baseline') {
    return undefined;
  }
  const seed =
    scenario === 'm04' ? M04_SEED : scenario === 'm05' ? M05_SEED : M06_SEED;
  return {
    seed,
    gateway: createLocalAgentGateway({
      agents: seed.agents.map((record) =>
        buildManagedAgent(record, seed.connections, seed.organizations),
      ),
      connections: seed.connections,
      organizations: seed.organizations,
    }),
  };
}
