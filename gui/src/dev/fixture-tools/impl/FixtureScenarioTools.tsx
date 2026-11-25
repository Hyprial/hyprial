/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14; re-expressed over
 * #2727):
 * - The scenario browser (the "当前旅程 / 归档场景" tabs, the two
 *   account/initialisation presets and the archived scenario selectors) is a
 *   fixture-only tool. It reads and writes the preview store itself and is
 *   handed to the floating tool layer as components, so the layer never
 *   imports scenario data. Markup, hooks and the URL mirror are the ones
 *   #2727 rendered inside DevPreviewApp.
 * - The selected tab is chosen when the layer mounts and survives closing
 *   and reopening the panel, as it did while it was DevPreviewApp state. The
 *   panel body is unmounted while the panel is closed, so the tab lives in
 *   FixtureScenarioScope, which the layer keeps mounted around itself.
 */
import {
  createContext,
  useContext,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { ArchivedScenarios } from '../../archive/ArchivedScenarios';
import {
  FIXTURE_PRESETS,
  applyFixtureScenario,
  authInitState,
  encodeFixtureId,
  getDevPreviewStore,
} from './preview-store';

type ScenarioTab = 'current' | 'archive';

interface ScenarioTabState {
  readonly tab: ScenarioTab;
  readonly setTab: (tab: ScenarioTab) => void;
}

const ScenarioTabContext = createContext<ScenarioTabState | null>(null);

export function FixtureScenarioScope({ children }: { readonly children: ReactNode }) {
  const devStore = getDevPreviewStore();
  const scenario = useSyncExternalStore(
    devStore.subscribeScenario,
    devStore.getScenario,
  );
  const [tab, setTab] = useState<ScenarioTab>(() => authInitState(scenario) ? 'current' : 'archive');
  const value = useMemo(() => ({ tab, setTab }), [tab]);
  return <ScenarioTabContext.Provider value={value}>{children}</ScenarioTabContext.Provider>;
}

function useScenarioTab(): ScenarioTabState {
  const state = useContext(ScenarioTabContext);
  if (!state) {
    throw new Error('fixture scenario tools must render inside FixtureScenarioScope');
  }
  return state;
}

export function FixtureScenarioBrowser() {
  const devStore = getDevPreviewStore();
  const scenario = useSyncExternalStore(
    devStore.subscribeScenario,
    devStore.getScenario,
  );
  const { tab, setTab } = useScenarioTab();
  const activeFixtureId = authInitState(scenario);
  return (
    <>
      <div className="dev-tools-tabs" role="tablist" aria-label="开发场景分类">
        {(['current', 'archive'] as const).map(value => (
          <button
            key={value}
            id={`dev-tools-tab-${value}`}
            role="tab"
            type="button"
            aria-selected={tab === value}
            aria-controls={`dev-tools-tabpanel-${value}`}
            tabIndex={tab === value ? 0 : -1}
            onClick={() => setTab(value)}
            onKeyDown={event => {
              if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              const next = event.key === 'Home' ? 'current' : event.key === 'End' ? 'archive'
                : value === 'current' ? 'archive' : 'current';
              setTab(next);
              document.getElementById(`dev-tools-tab-${next}`)?.focus();
            }}
          >
            {value === 'current' ? '当前旅程' : '归档场景'}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`dev-tools-tabpanel-${tab}`} aria-labelledby={`dev-tools-tab-${tab}`}>
        {tab === 'archive' ? <ArchivedScenarios scenario={scenario} encodeScenario={encodeFixtureId} /> : (
          <div aria-label="账号与初始化状态" className="dev-tools-presets" role="group">
            {FIXTURE_PRESETS.map((preset) => (
              <button
                aria-pressed={activeFixtureId === preset.id}
                className="dev-tools-scenario-preset"
                data-dev-scenario-active={activeFixtureId === preset.id ? 'true' : undefined}
                data-dev-scenario-control=""
                data-dev-scenario-value={preset.id}
                key={preset.id}
                onClick={() => applyFixtureScenario(preset.patch)}
                type="button"
              >
                {preset.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
