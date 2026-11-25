/**
 * Historical fixture selectors remain development-only. Draft changes never
 * mutate the active preview store; a separate tab loads the shared App with
 * the existing scenario URL contract. These are data references, not snapshots
 * of historical product layouts or evidence of backend readiness.
 */
import { useState } from 'react';
import type { DevScenarioState } from '../fixture-tools';
import {
  DevAgentScenarioSelector,
  DevChatScenarioSelector,
  DevMachineScenarioSelector,
  DevOrgScenarioSelector,
  DevScenarioSelector,
  DevTopologyScenarioSelector,
} from '../DevScenarioSelector';
import { isOrgDevScenario, ORG_DEV_SCENARIOS } from '../org/scenarios';

export function ArchivedScenarios({ scenario, encodeScenario }: {
  readonly scenario: DevScenarioState;
  readonly encodeScenario: (state: DevScenarioState) => string;
}) {
  const [draft, setDraft] = useState<DevScenarioState>(() => (
    scenario.j1 === 'logged-in-init' || scenario.j1 === 'logged-out-init'
      ? {
          j1: 'account-ready', j2: 'baseline', j3: 'baseline', j5: 'baseline',
          j6: 'baseline', j7: 'baseline', orgData: 'off', j4: 'ready', j10: 'ready',
        }
      : { ...scenario }
  ));
  const update = (patch: Partial<DevScenarioState>) => setDraft(current => ({ ...current, ...patch }));
  const params = new URLSearchParams({
    hyprialScenario: encodeScenario(draft),
    hyprialFirstRun: 'true',
  });

  return (
    <div className="dev-tools-scenarios">
      <p className="dev-tools-archive-note">历史模拟数据，仅供参考。新标签页打开，不影响当前旅程。</p>
      <DevScenarioSelector value={draft.j1} onChange={j1 => update({ j1 })} />
      <DevAgentScenarioSelector value={draft.j2} onChange={j2 => update({ j2 })} />
      <DevChatScenarioSelector value={draft.j3} onChange={j3 => update({ j3 })} />
      <DevOrgScenarioSelector value={draft.j5} onChange={j5 => update({ j5 })} />
      <DevMachineScenarioSelector value={draft.j6} onChange={j6 => update({ j6 })} />
      <DevTopologyScenarioSelector value={draft.j7} onChange={j7 => update({ j7 })} />
      <label className="dev-scenario" htmlFor="dev-archive-org-data">
        <span>组织数据</span>
        <select id="dev-archive-org-data" value={draft.orgData} onChange={event => {
          if (isOrgDevScenario(event.target.value)) update({ orgData: event.target.value });
        }}>
          {ORG_DEV_SCENARIOS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
      <a className="dev-tools-archive-link" href={`?${params}`} target="_blank" rel="noopener noreferrer">
        打开归档场景 ↗
      </a>
    </div>
  );
}
