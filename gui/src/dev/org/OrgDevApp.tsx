import { useMemo } from 'react';
import type { ChatSeed, FirstRunMode } from '../../state/types';
import type { StateBaseline } from '../../state/appReducer';
import { App } from '../../App';
import {
  FIXTURE_AGENTS,
  FIXTURE_CONNECTIONS,
  FIXTURE_THREADS,
} from '../fixtures/demo';
import { createFixtureCapabilities } from '../first-run-capabilities';
import { useChatContractRuntime } from '../mock/use-contract-runtime';
import type { AgentChatScenario } from '../mock/agent-chat';
import { OrgDomainProvider } from '../../features/org';
import {
  ORG_DEV_SCENARIOS,
  buildOrgDevPreview,
  isOrgDevScenario,
  type OrgDevScenario,
} from './scenarios';

/**
 * 开发定位层工具条上的“组织数据”选择。默认“未挂数据源”，此时开发定位层保持原样。
 * 不带 data-mock-scenario：场景选择器另行计数，这里用 data-org-dev-data 标记。
 */
export function DevOrgDataSelector({
  value,
  onChange,
}: {
  readonly value: OrgDevScenario;
  readonly onChange: (value: OrgDevScenario) => void;
}) {
  return (
    <label className="dev-scenario" htmlFor="dev-org-data">
      <span>组织数据</span>
      <select
        id="dev-org-data"
        data-org-dev-data=""
        value={value}
        onChange={(event) => {
          if (isOrgDevScenario(event.target.value)) {
            onChange(event.target.value);
          }
        }}
      >
        {ORG_DEV_SCENARIOS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

const ORG_DEV_BASELINE: StateBaseline = {
  targets: FIXTURE_AGENTS,
  connections: FIXTURE_CONNECTIONS,
  threads: FIXTURE_THREADS,
};

/**
 * 选中组织数据时的组合：用 OrgDomainProvider 包住同一个产品 App，并带上配套的
 * Agent、聊天开发数据。所有开发输入（数据源、能力适配器、契约场景）都从这里
 * 注入；产品树、路由与首次设置门不感知开发模式。
 */
export function OrgDevApp({
  scenario,
  firstRunMode = 'complete',
  firstRunWizardEnabled = true,
  chatScenario,
  chatSeed,
}: {
  readonly scenario: Exclude<OrgDevScenario, 'off'>;
  readonly firstRunMode?: FirstRunMode;
  readonly firstRunWizardEnabled?: boolean;
  readonly chatScenario?: AgentChatScenario;
  readonly chatSeed?: ChatSeed;
}) {
  const preview = useMemo(() => buildOrgDevPreview(scenario), [scenario]);
  const chatContract = useChatContractRuntime(chatScenario);
  const mockCapabilitiesFactory = useMemo(
    () => createFixtureCapabilities({
      firstRunMode,
      firstRunWizardEnabled,
      orgSource: preview?.source,
    }),
    [firstRunMode, firstRunWizardEnabled, preview],
  );
  if (!preview) {
    return null;
  }
  return (
    <OrgDomainProvider key={scenario} source={preview.source}>
      <App
        key={scenario}
        baseline={ORG_DEV_BASELINE}
        firstRunMode={firstRunMode}
        capabilitiesFactory={mockCapabilitiesFactory}
        chatContract={chatContract}
        chatSeed={chatSeed ?? preview.chatSeed}
        agentSeed={preview.agentSeed}
        agentGateway={preview.agentGateway}
      />
    </OrgDomainProvider>
  );
}
