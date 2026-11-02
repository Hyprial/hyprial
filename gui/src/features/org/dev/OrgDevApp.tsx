import { useMemo } from 'react';
import type { ChatSeed, FirstRunMode } from '../../../state/types';
import type { AgentChatScenario } from '../../agent-chat/api/mock';
import { App } from '../../../App';
import type { GuiPage } from '../../../state/gui-view-model';
import type { DevModuleId } from '../../../dev-mode/module-registry';
import type { AgentProviderScenario } from '../../agent-provider/api/mock';
import type { AccountLoginScenario } from '../../../dev-mode/first-run-capabilities';
import { OrgDomainProvider } from '../domain/context';
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

/**
 * 选中组织数据时的 App 组合：用 OrgDomainProvider 包住 App，并带上配套的
 * Agent、聊天开发数据。首次设置视为已完成，直接进入开始页。
 */
export function OrgDevApp({
  scenario,
  agentProviderScenario,
  accountLoginScenario,
  agentChatScenario,
  firstRunMode = 'complete',
  chatSeed,
  devLabels,
}: {
  readonly scenario: Exclude<OrgDevScenario, 'off'>;
  readonly agentChatScenario?: AgentChatScenario;
  readonly firstRunMode?: FirstRunMode;
  readonly chatSeed?: ChatSeed;
  readonly agentProviderScenario?: AgentProviderScenario;
  readonly accountLoginScenario?: AccountLoginScenario;
  readonly devLabels?: Readonly<Record<GuiPage, DevModuleId>>;
}) {
  const preview = useMemo(() => buildOrgDevPreview(scenario), [scenario]);
  if (!preview) {
    return null;
  }
  return (
    <OrgDomainProvider key={scenario} source={preview.source}>
      <App
        agentProviderScenario={agentProviderScenario}
        accountLoginScenario={accountLoginScenario}
        key={scenario}
        dataMode="fixture"
        firstRunMode={firstRunMode}
        agentChatScenario={agentChatScenario}
        agentSeed={preview.agentSeed}
        agentGateway={preview.agentGateway}
        chatSeed={chatSeed ?? preview.chatSeed}
        devLabels={devLabels}
      />
    </OrgDomainProvider>
  );
}
