import { useReducer, useRef, useState } from 'react';
import { App } from '../App';
import { DEV_MODULE_BY_PAGE } from './module-registry';
import { DevDataSourceNotice, DevModuleIndex } from './DevModuleIndex';
import {
  DevAgentScenarioSelector,
  DevChatScenarioSelector,
  DevMachineScenarioSelector,
  DevOrgScenarioSelector,
  DevScenarioSelector,
  DevTopologyScenarioSelector,
  isJ1Scenario,
  providerScenarioForJ1,
  accountScenarioForJ1,
  firstRunModeForJ1,
  type J1Scenario,
} from './DevScenarioSelector';
import { buildAgentScenario, isJ2Scenario, type J2Scenario } from './j2-scenarios';
import { buildChatSeed, chatContractScenarioForJ3, isJ3Scenario, type J3Scenario } from './chat-scenarios';
import { buildOrgScenario, isJ5Scenario, type J5Scenario } from './org-scenarios';
import { buildMachineScenario, isJ6Scenario, type J6Scenario } from './machine-scenarios';
import { buildTopologyScenario, isJ7Scenario, type J7Scenario } from './topology-scenarios';
import { SessionToolbar } from './SessionToolbar';
import { DevOrgDataSelector, OrgDevApp } from '../features/org/dev/OrgDevApp';
import type { OrgDevScenario } from '../features/org/dev/scenarios';
import { createInitialSession } from './session/initial-session';
import {
  applyJ10Scenario,
  applyJ4Scenario,
  isJ10Scenario,
  isJ4Scenario,
  type J10Scenario,
  type J4Scenario,
} from './remaining-scenarios';
import { sessionReducer } from './session/session-reducer';
import type { LinkedSession, SessionAction } from './session/session-types';
import './dev-module-index.css';

function hashValue(key: string): string | null {
  return new URLSearchParams(window.location.hash.slice(1)).get(key);
}

export function DevPreviewApp() {
  // Regression injection: the legacy scenario dropdowns re-mount the app by key.
  const [regression, setRegression] = useState<{
    j1: J1Scenario;
    j3: J3Scenario;
    j5: J5Scenario;
    j2: J2Scenario;
    j6: J6Scenario;
    j7: J7Scenario;
  }>(() => ({
    j1: isJ1Scenario(hashValue('j1') ?? '') ? (hashValue('j1') as J1Scenario) : 'simulate',
    j3: isJ3Scenario(hashValue('j3') ?? '') ? (hashValue('j3') as J3Scenario) : 'baseline',
    j5: isJ5Scenario(hashValue('j5') ?? '') ? (hashValue('j5') as J5Scenario) : 'baseline',
    j2: isJ2Scenario(hashValue('j2') ?? '') ? (hashValue('j2') as J2Scenario) : 'baseline',
    j6: isJ6Scenario(hashValue('j6') ?? '') ? (hashValue('j6') as J6Scenario) : 'baseline',
    j7: isJ7Scenario(hashValue('j7') ?? '') ? (hashValue('j7') as J7Scenario) : 'baseline',
  }));

  // dev-04 组织数据（姚升悦 2026-10-30 授权接入）：选中后用组织数据源包住 App，默认不挂。
  const [orgData, setOrgData] = useState<OrgDevScenario>('off');
  const [j10Scenario] = useState<J10Scenario>(() => {
    const value = new URLSearchParams(window.location.hash.slice(1)).get('j10');
    return value !== null && isJ10Scenario(value) ? value : 'ready';
  });
  const [j4Scenario] = useState<J4Scenario>(() => {
    const value = new URLSearchParams(window.location.hash.slice(1)).get('j4');
    return value !== null && isJ4Scenario(value) ? value : 'ready';
  });
  const [session, dispatchSession] = useReducer(
    sessionReducer,
    undefined,
    () =>
      applyJ4Scenario(
        applyJ10Scenario(createInitialSession(), j10Scenario),
        j4Scenario,
      ),
  );
  const sessionRef = useRef<LinkedSession>(session);
  sessionRef.current = session;

  const action = (next: SessionAction) => dispatchSession(next);

  // Regression injection: any non-default legacy selector renders the old
  // keyed combination instead of the continuous session.
  const legacyActive =
    regression.j1 !== 'simulate' ||
    regression.j3 !== 'baseline' ||
    regression.j5 !== 'baseline' ||
    regression.j2 !== 'baseline' ||
    regression.j6 !== 'baseline' ||
    regression.j7 !== 'baseline';

  const providerScenario = providerScenarioForJ1(regression.j1) ?? 'success';
  const accountScenario = accountScenarioForJ1(regression.j1);
  const chatScenario = chatContractScenarioForJ3(regression.j3);
  const firstRunMode = chatScenario || (regression.j1 === 'simulate' && regression.j3 !== 'baseline')
    ? 'complete'
    : firstRunModeForJ1(regression.j1);
  const regressionKey = `${regression.j1}:${regression.j3}:${regression.j5}:${regression.j2}:${regression.j6}:${regression.j7}`;
  const legacyAgent = buildAgentScenario(regression.j2);
  const legacyMachine = buildMachineScenario(regression.j6);
  const legacyOrg = buildOrgScenario(regression.j5);

  return (
    <div className="dev-preview-frame">
      <div className="dev-preview-toolbar">
        <DevDataSourceNotice />
        <DevScenarioSelector
          value={regression.j1}
          onChange={(j1) => setRegression({ ...regression, j1 })}
        />
        <DevChatScenarioSelector
          value={regression.j3}
          onChange={(j3) => setRegression({ ...regression, j3 })}
        />
        <DevOrgScenarioSelector
          value={regression.j5}
          onChange={(j5) => setRegression({ ...regression, j5 })}
        />
        <DevOrgDataSelector value={orgData} onChange={setOrgData} />
        <DevAgentScenarioSelector
          value={regression.j2}
          onChange={(j2) => setRegression({ ...regression, j2 })}
        />
        <DevMachineScenarioSelector
          value={regression.j6}
          onChange={(j6) => setRegression({ ...regression, j6 })}
        />
        <DevTopologyScenarioSelector
          value={regression.j7}
          onChange={(j7) => setRegression({ ...regression, j7 })}
        />
      </div>
      {orgData !== 'off' ? (
        <OrgDevApp
          key={`${orgData}:${regressionKey}`}
          scenario={orgData}
          firstRunMode={regression.j1 === 'simulate' ? 'complete' : firstRunMode}
          agentChatScenario={chatScenario}
          chatSeed={buildChatSeed(regression.j3)}
          agentProviderScenario={providerScenario}
          accountLoginScenario={accountScenario}
          devLabels={__HYPRIAL_DEV_MODULE_LABELS__ ? DEV_MODULE_BY_PAGE : undefined}
        />
      ) : legacyActive ? (
        <App
          agentProviderScenario={providerScenario}
          accountLoginScenario={accountScenario}
          key={regressionKey}
          dataMode="fixture"
          firstRunMode={firstRunMode}
          agentChatScenario={chatScenario}
          chatSeed={buildChatSeed(regression.j3)}
          orgModel={legacyOrg}
          agentSeed={legacyAgent?.seed}
          agentGateway={legacyAgent?.gateway}
          machineModel={legacyMachine}
          topologyModel={buildTopologyScenario(regression.j7, {
            members: legacyOrg?.members ?? [],
            machines: legacyMachine?.machines ?? [],
          })}
          devLabels={
            __HYPRIAL_DEV_MODULE_LABELS__ ? DEV_MODULE_BY_PAGE : undefined
          }
        />
      ) : (
        <App
          agentProviderScenario={providerScenario}
          accountLoginScenario={accountScenario}
          key="linked"
          dataMode="fixture"
          session={session}
          onSessionAction={action}
          devLabels={
            __HYPRIAL_DEV_MODULE_LABELS__ ? DEV_MODULE_BY_PAGE : undefined
          }
        />
      )}
      <div className="dev-preview-footer">
        {legacyActive || orgData !== 'off' ? null : (
          <SessionToolbar session={session} onAction={action} />
        )}
        <DevModuleIndex />
      </div>
    </div>
  );
}
