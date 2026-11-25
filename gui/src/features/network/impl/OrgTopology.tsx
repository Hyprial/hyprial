import { Button } from '../../../components/primitives';
import { useCallback, useMemo, useRef, useState } from 'react';
import { TopologyDiagram } from './TopologyDiagram';
import { InspectPanel, MonitorPanel } from './TopologyPanels';
import {
  TOPOLOGY_COPY,
  type TopologyModel,
  type TopologyView,
} from './topology-view-model';
import './topology.css';

type PanelState = 'none' | 'inspect' | 'monitor';

export function OrgTopology({
  model,
  onRetry,
}: {
  readonly model: TopologyModel;
  readonly onRetry?: () => void;
}) {
  const [view, setView] = useState<TopologyView>('structure');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [panel, setPanel] = useState<PanelState>('none');
  const monitorRef = useRef<HTMLButtonElement>(null);

  const edge = useMemo(
    () => model.edges.find((item) => item.id === selectedEdgeId) ?? null,
    [model.edges, selectedEdgeId],
  );

  const closePanel = useCallback(() => {
    setPanel('none');
    const trigger = selectedEdgeId
      ? document.querySelector<HTMLElement>(
          `[data-topology-edge="${selectedEdgeId}"]`,
        )
      : monitorRef.current;
    trigger?.focus();
  }, [selectedEdgeId]);

  if (model.dataMode === 'hidden') {
    return null;
  }

  if (model.dataMode === 'unconfirmed') {
    return (
      <section
        className="topology-section org-topology"
        aria-labelledby="topology-title"
        data-topology-mode={model.dataMode}
      >
        <h2 id="topology-title" className="org-network-title">{TOPOLOGY_COPY.title}</h2>
        <p className="topology-state" role="status">
          {model.unavailableMessage}
        </p>
        <Button variant="secondary" type="button" onClick={onRetry}>
          {TOPOLOGY_COPY.retry}
        </Button>
      </section>
    );
  }

  const showDiagram = model.dataMode !== 'empty' || model.nodes.length > 0;
  const hasCommunication = model.edges.length > 0 && model.dataMode !== 'structure-only';
  const activeView: TopologyView = hasCommunication ? view : 'structure';
  const monitorEnabled = model.monitor.mode === 'linked';

  return (
    <section
      className="topology-section org-topology"
      aria-labelledby="topology-title"
      data-topology-mode={model.dataMode}
    >
      <h2 id="topology-title" className="org-network-title">{TOPOLOGY_COPY.title}</h2>

      {hasCommunication || monitorEnabled ? (
      <div className="topology-toolbar">
        {hasCommunication ? (
          <div className="topology-views" role="group" aria-label="拓扑视图">
            <Button variant="secondary"
              type="button"
              data-topology-view="structure"
              aria-pressed={view === 'structure'}
              onClick={() => setView('structure')}
            >
              {TOPOLOGY_COPY.structureView}
            </Button>
            <Button variant="secondary"
              type="button"
              data-topology-view="communication"
              aria-pressed={view === 'communication'}
              onClick={() => setView('communication')}
            >
              {TOPOLOGY_COPY.communicationView}
            </Button>
          </div>
        ) : null}
        {monitorEnabled ? (
          <Button variant="secondary"
            ref={monitorRef}
            type="button"
            aria-pressed={panel === 'monitor'}
            onClick={() => setPanel(panel === 'monitor' ? 'none' : 'monitor')}
          >
            Monitor
          </Button>
        ) : null}
      </div>
      ) : null}

      {model.emptyMessage ? (
        <p className="topology-state">{model.emptyMessage}</p>
      ) : null}

      {showDiagram ? (
        <TopologyDiagram
          model={model}
          view={activeView}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          onSelectNode={setSelectedNodeId}
          onSelectEdge={(id) => {
            setSelectedEdgeId(id);
            setPanel('inspect');
          }}
        />
      ) : null}

      {panel === 'inspect' && edge ? (
        <InspectPanel
          edge={edge}
          nodes={model.nodes}
          observedAt={model.monitor.sample?.observedAt ?? ''}
          onClose={closePanel}
        />
      ) : null}

      {panel === 'monitor' && monitorEnabled ? (
        <MonitorPanel monitor={model.monitor} onClose={closePanel} />
      ) : null}
    </section>
  );
}
