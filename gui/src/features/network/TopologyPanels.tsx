import { useEffect, useRef } from 'react';
import {
  COMMUNICATION_STATUS_LABELS,
  type MonitorModel,
  type TopologyEdge,
  type TopologyNode,
} from './topology-view-model';

function usePanelFocus(
  headingRef: React.RefObject<HTMLHeadingElement | null>,
  onClose: () => void,
) {
  useEffect(() => {
    headingRef.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [headingRef, onClose]);
}

function nodeLabel(nodes: readonly TopologyNode[], id: string): string {
  return nodes.find((node) => node.id === id)?.label ?? id;
}

export function InspectPanel({
  edge,
  nodes,
  observedAt,
  onClose,
}: {
  readonly edge: TopologyEdge;
  readonly nodes: readonly TopologyNode[];
  readonly observedAt: string;
  readonly onClose: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  usePanelFocus(headingRef, onClose);

  return (
    <aside
      className="topology-panel"
      data-topology-panel="inspect"
      role="dialog"
      aria-labelledby="topology-inspect-title"
    >
      <div className="topology-panel-head">
        <h3 id="topology-inspect-title" ref={headingRef} tabIndex={-1}>
          通信详情 · Inspect
        </h3>
        <button
          type="button"
          className="button button-secondary"
          aria-label="关闭 Inspect"
          onClick={onClose}
        >
          关闭
        </button>
      </div>
      <dl className="topology-panel-fields">
        <dt>发起方</dt>
        <dd>{nodeLabel(nodes, edge.fromId)}</dd>
        <dt>接收方</dt>
        <dd>{nodeLabel(nodes, edge.toId)}</dd>
        <dt>状态</dt>
        <dd>{COMMUNICATION_STATUS_LABELS[edge.status]}</dd>
        <dt>时间</dt>
        <dd>{observedAt}</dd>
      </dl>
    </aside>
  );
}

export function MonitorPanel({
  monitor,
  onClose,
}: {
  readonly monitor: MonitorModel;
  readonly onClose: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  usePanelFocus(headingRef, onClose);

  return (
    <aside
      className="topology-panel"
      data-topology-panel="monitor"
      role="dialog"
      aria-labelledby="topology-monitor-title"
    >
      <div className="topology-panel-head">
        <h3 id="topology-monitor-title" ref={headingRef} tabIndex={-1}>
          {monitor.title}
        </h3>
        <button
          type="button"
          className="button button-secondary"
          aria-label="关闭 Monitor"
          onClick={onClose}
        >
          关闭
        </button>
      </div>
      {monitor.sample ? (
        <dl className="topology-panel-fields">
          <dt>发起方</dt>
          <dd>{monitor.sample.fromLabel}</dd>
          <dt>接收方</dt>
          <dd>{monitor.sample.toLabel}</dd>
          <dt>状态</dt>
          <dd>{COMMUNICATION_STATUS_LABELS[monitor.sample.status]}</dd>
          <dt>时间</dt>
          <dd>{monitor.sample.observedAt}</dd>
        </dl>
      ) : null}
    </aside>
  );
}
