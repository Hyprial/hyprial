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

  if (model.dataMode === 'unavailable' || model.dataMode === 'unconfirmed') {
    return (
      <section
        className="card topology-section org-topology"
        aria-labelledby="topology-title"
        data-topology-mode={model.dataMode}
      >
        <div className="section-heading">
          <div>
            <h2 id="topology-title">{TOPOLOGY_COPY.title}</h2>
          </div>
        </div>
        <p className="topology-state" role="status">
          {model.unavailableMessage}
        </p>
        {model.dataMode === 'unconfirmed' ? (
          <button type="button" className="button button-secondary" onClick={onRetry}>
            {TOPOLOGY_COPY.retry}
          </button>
        ) : null}
      </section>
    );
  }

  const showDiagram = model.dataMode !== 'empty' || model.nodes.length > 0;
  const communicationUnavailable =
    model.edges.length === 0 || model.dataMode === 'structure-only';
  const monitorEnabled = model.monitor.mode === 'linked';

  return (
    <section
      className="card topology-section org-topology"
      aria-labelledby="topology-title"
      data-topology-mode={model.dataMode}
    >
      <div className="section-heading">
        <div>
          <h2 id="topology-title">{TOPOLOGY_COPY.title}</h2>
          <p>成员与机器之间的连接关系。</p>
        </div>
      </div>

      <div className="topology-toolbar">
        <div className="topology-views" role="group" aria-label="拓扑视图">
          <button
            type="button"
            className="button button-secondary"
            data-topology-view="structure"
            aria-pressed={view === 'structure'}
            onClick={() => setView('structure')}
          >
            {TOPOLOGY_COPY.structureView}
          </button>
          <button
            type="button"
            className="button button-secondary"
            data-topology-view="communication"
            aria-pressed={view === 'communication'}
            onClick={() => setView('communication')}
          >
            {TOPOLOGY_COPY.communicationView}
          </button>
        </div>
        <button
          ref={monitorRef}
          type="button"
          className="button button-secondary"
          aria-pressed={panel === 'monitor'}
          disabled={!monitorEnabled}
          onClick={() => setPanel(panel === 'monitor' ? 'none' : 'monitor')}
        >
          Monitor
        </button>
      </div>

      {!monitorEnabled ? (
        <p className="topology-state">{TOPOLOGY_COPY.monitorUnavailable}</p>
      ) : null}

      {model.emptyMessage ? (
        <p className="topology-state">{model.emptyMessage}</p>
      ) : null}

      {showDiagram ? (
        <TopologyDiagram
          model={model}
          view={view}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          onSelectNode={setSelectedNodeId}
          onSelectEdge={(id) => {
            setSelectedEdgeId(id);
            setPanel('inspect');
          }}
        />
      ) : null}

      {view === 'communication' && communicationUnavailable ? (
        <p className="topology-state">{TOPOLOGY_COPY.communicationUnavailable}</p>
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
