import {
  COMMUNICATION_STATUS_LABELS,
  TOPOLOGY_COPY,
  type TopologyEdge,
  type TopologyModel,
  type TopologyNode,
  type TopologyView,
} from './topology-view-model';

function nodeShape(node: TopologyNode): string {
  return node.kind === 'member' ? 'member' : 'machine';
}

export function TopologyDiagram({
  model,
  view,
  selectedNodeId,
  selectedEdgeId,
  onSelectNode,
  onSelectEdge,
}: {
  readonly model: TopologyModel;
  readonly view: TopologyView;
  readonly selectedNodeId: string | null;
  readonly selectedEdgeId: string | null;
  readonly onSelectNode: (id: string) => void;
  readonly onSelectEdge: (id: string) => void;
}) {
  const edges = view === 'communication' ? model.edges : [];

  return (
    <div className="topology-diagram-scroll">
      <div className="topology-diagram" data-topology-diagram={view}>
        <ul className="topology-nodes" aria-label="拓扑节点">
          {model.nodes.map((node) => (
            <li key={node.id}>
              <button
                type="button"
                className={`topology-node topology-node--${nodeShape(node)}`}
                data-topology-node={node.id}
                aria-pressed={selectedNodeId === node.id}
                onClick={() => onSelectNode(node.id)}
              >
                <span className="topology-node-label">{node.label}</span>
                <span className="topology-node-meta">{node.meta}</span>
              </button>
            </li>
          ))}
        </ul>

        {view === 'communication' ? (
          <ul className="topology-edges" aria-label="通信连接">
            {edges.map((edge: TopologyEdge) => (
              <li key={edge.id}>
                <button
                  type="button"
                  className="topology-edge"
                  data-topology-edge={edge.id}
                  aria-pressed={selectedEdgeId === edge.id}
                  onClick={() => onSelectEdge(edge.id)}
                >
                  <span className="topology-edge-arrow" aria-hidden="true">
                    →
                  </span>
                  <span>
                    {edge.fromId} → {edge.toId} ·{' '}
                    {COMMUNICATION_STATUS_LABELS[edge.status]}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      <ul className="topology-legend" aria-label="拓扑图例">
        <li>{TOPOLOGY_COPY.legend.member}</li>
        <li>{TOPOLOGY_COPY.legend.machine}</li>
        {view === 'communication' ? <li>{TOPOLOGY_COPY.legend.edge}</li> : null}
      </ul>
    </div>
  );
}
