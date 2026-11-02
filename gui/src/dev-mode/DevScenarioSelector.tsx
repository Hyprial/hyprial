import type { FirstRunMode } from '../state/types';
import type { AgentProviderScenario } from '../features/agent-provider/api/mock';
import type { AccountLoginScenario } from './first-run-capabilities';
import { J3_SCENARIOS, isJ3Scenario, type J3Scenario } from './chat-scenarios';
import { J5_SCENARIOS, isJ5Scenario, type J5Scenario } from './org-scenarios';
import { J2_SCENARIOS, isJ2Scenario, type J2Scenario } from './j2-scenarios';
import { J6_SCENARIOS, isJ6Scenario, type J6Scenario } from './machine-scenarios';
import { J7_SCENARIOS, isJ7Scenario, type J7Scenario } from './topology-scenarios';

export const J1_SCENARIOS = [
  { value: 'simulate', label: '成功流程' },
  { value: 'account-failure', label: '账号失败' },
  { value: 'model-failure', label: '模型失败' },
  { value: 'squire-failure', label: 'Squire 失败' },
  { value: 'conversation-failure', label: '对话失败' },
  { value: 'model-unavailable', label: '模型不可用' },
  { value: 'complete', label: '首次设置已完成' },
  { value: 'provider-success', label: 'Agent provider · 成功' },
  { value: 'provider-empty', label: 'Agent provider · 空数据' },
  { value: 'provider-error', label: 'Agent provider · 错误' },
  { value: 'provider-unconfigured', label: 'Agent provider · 未配置' },
  { value: 'account-success', label: '账号登录 · 成功' },
  { value: 'account-empty', label: '账号登录 · 未登录' },
  { value: 'account-error', label: '账号登录 · 错误' },
  { value: 'account-disabled', label: '账号登录 · 禁用' },
] as const satisfies readonly {
  readonly value: FirstRunMode | `provider-${AgentProviderScenario}` | `account-${AccountLoginScenario}`;
  readonly label: string;
}[];

export type J1Scenario = (typeof J1_SCENARIOS)[number]['value'];

export function firstRunModeForJ1(scenario: J1Scenario): FirstRunMode {
  return providerScenarioForJ1(scenario) || accountScenarioForJ1(scenario) ? 'complete' : scenario as FirstRunMode;
}

export function isJ1Scenario(value: string): value is J1Scenario {
  return J1_SCENARIOS.some((option) => option.value === value);
}

export function providerScenarioForJ1(scenario: J1Scenario): AgentProviderScenario | undefined {
  switch (scenario) {
    case 'provider-success': return 'success';
    case 'provider-empty': return 'empty';
    case 'provider-error': return 'error';
    case 'provider-unconfigured': return 'unconfigured';
    default: return undefined;
  }
}

export function accountScenarioForJ1(scenario: J1Scenario): AccountLoginScenario | undefined {
  switch (scenario) {
    case 'account-success': return 'success';
    case 'account-empty': return 'empty';
    case 'account-error': return 'error';
    case 'account-disabled': return 'disabled';
    default: return undefined;
  }
}

export function DevScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J1Scenario;
  readonly onChange: (value: J1Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j1-scenario">
      <span>J1 场景</span>
      <select
        id="dev-j1-scenario"
        data-mock-scenario="j1"
        value={value}
        onChange={(event) => {
          if (isJ1Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J1_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DevChatScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J3Scenario;
  readonly onChange: (value: J3Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j3-scenario">
      <span>J3 场景</span>
      <select
        id="dev-j3-scenario"
        data-mock-scenario="j3"
        value={value}
        onChange={(event) => {
          if (isJ3Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J3_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DevOrgScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J5Scenario;
  readonly onChange: (value: J5Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j5-scenario">
      <span>J5 场景</span>
      <select
        id="dev-j5-scenario"
        data-mock-scenario="j5"
        value={value}
        onChange={(event) => {
          if (isJ5Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J5_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DevAgentScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J2Scenario;
  readonly onChange: (value: J2Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j2-scenario">
      <span>J2 场景</span>
      <select
        id="dev-j2-scenario"
        data-mock-scenario="j2"
        value={value}
        onChange={(event) => {
          if (isJ2Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J2_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DevMachineScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J6Scenario;
  readonly onChange: (value: J6Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j6-scenario">
      <span>J6 场景</span>
      <select
        id="dev-j6-scenario"
        data-mock-scenario="j6"
        value={value}
        onChange={(event) => {
          if (isJ6Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J6_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DevTopologyScenarioSelector({
  value,
  onChange,
}: {
  readonly value: J7Scenario;
  readonly onChange: (value: J7Scenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-j7-scenario">
      <span>J7 场景</span>
      <select
        id="dev-j7-scenario"
        data-mock-scenario="j7"
        value={value}
        onChange={(event) => {
          if (isJ7Scenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {J7_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
