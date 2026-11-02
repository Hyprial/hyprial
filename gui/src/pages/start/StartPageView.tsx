import type { ReactNode } from 'react';
import { Badge } from '../../components/Badge';
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  UnsupportedState,
} from '../../components/primitives';
import type {
  StartNodeViewModel,
  StartPageActions,
  StartPageDataViewModel,
  StartPageViewModel,
} from './start-view-model';
import './start-page.css';

export interface StartPageViewProps {
  readonly view: StartPageViewModel;
  readonly mobile: boolean;
  readonly actions: StartPageActions;
  /**
   * Container-owned first-run surface. It keeps its model hooks and actions in
   * LiveApp instead of leaking live operations into this display component.
   */
  readonly firstRun?: ReactNode;
  /**
   * Container-owned host-session entry. It is shown when the real directory
   * already exposes Squire, so setup can end in the normal authorized session
   * instead of requiring a manual trip through the Agent list.
   */
  readonly sessionEntry?: ReactNode;
}

function NodeSection({
  state,
  onRetry,
}: {
  readonly state: StartPageDataViewModel['nodes'];
  readonly onRetry: () => void;
}) {
  if (state.status === 'loading') {
    return <LoadingState title="正在加载节点" />;
  }
  if (state.status === 'error') {
    return (
      <ErrorState
        message={state.message}
        code={state.code}
        retryable={state.retryable}
        retryLabel="重试本机请求"
        onRetry={onRetry}
      />
    );
  }
  if (state.status === 'unsupported') {
    return <UnsupportedState reason={state.reason} code={state.code} />;
  }
  if (state.status === 'empty') {
    return (
      <EmptyState
        title={state.title ?? '暂无网络节点'}
        description={
          state.description ?? '当前 daemon 未返回任何节点。'
        }
      />
    );
  }
  if (state.data.length === 0) {
    return (
      <EmptyState
        title="暂无网络节点"
        description="当前 daemon 未返回任何节点。"
      />
    );
  }
  return <NodeList nodes={state.data} />;
}

function NodeList({ nodes }: { readonly nodes: readonly StartNodeViewModel[] }) {
  return (
    <ul className="live-node-list" aria-label="网络节点列表">
      {nodes.map((node) => (
        <li key={node.nodeId} className="live-node-row">
          <strong>{node.nodeId}</strong>
          <Badge tone="neutral">{node.status}</Badge>
        </li>
      ))}
    </ul>
  );
}

function ReadyStartPage({
  data,
  actions,
  firstRun,
  sessionEntry,
}: {
  readonly data: StartPageDataViewModel;
  readonly actions: StartPageActions;
  readonly firstRun?: ReactNode;
  readonly sessionEntry?: ReactNode;
}) {
  return (
    <div className="start-bento">
      {firstRun}
      {sessionEntry}
      <div className="metrics" role="group" aria-label="实时概览">
        {data.metrics.map((metric) => (
          <Card
            as="article"
            padding="md"
            className="card metric"
            data-summary={metric.id}
            key={metric.id}
          >
            <span className="metric-label">{metric.label}</span>
            <strong
              className="metric-value"
              data-unknown={metric.value === null ? 'true' : undefined}
            >
              {metric.value === null ? '—' : metric.value}
            </strong>
          </Card>
        ))}
      </div>

      <Card
        as="section"
        padding="md"
        className="card recent-card"
        aria-labelledby="instance-title"
      >
        <span className="eyebrow">{data.instance.eyebrow}</span>
        <h2 className="section-title" id="instance-title">
          {data.instance.title}
        </h2>
        <p className="copy">{data.instance.description}</p>
        <p className="live-code">{data.instance.metadata}</p>
        <Button
          variant="secondary"
          className="button button-secondary"
          onClick={actions.openChat}
        >
          打开聊天
        </Button>
      </Card>

      <Card
        as="section"
        padding="md"
        className="card recent-card"
        aria-labelledby="nodes-title"
      >
        <h2 className="section-title" id="nodes-title">
          网络节点
        </h2>
        <NodeSection state={data.nodes} onRetry={actions.retry} />
      </Card>

      <Card
        as="section"
        padding="md"
        className="card board-card"
        aria-labelledby="board-title"
      >
        <h2 className="section-title" id="board-title">
          当前看板
        </h2>
        <UnsupportedState reason={data.board.reason} code={data.board.code} />
      </Card>

      <Card
        as="section"
        padding="md"
        className="card recommendation-card"
        aria-labelledby="recommendation-title"
      >
        <span className="eyebrow">下一步</span>
        <h2 id="recommendation-title">从真实 Agent 目录开始</h2>
        <p>
          选择一个 Agent 会经本机服务创建或复用会话；如果当前版本缺少类型化接口，
          页面会明确显示 unsupported。
        </p>
        <Button
          variant="primary"
          className="button button-strong"
          onClick={actions.openAgents}
        >
          查看 Agent 目录
        </Button>
      </Card>
    </div>
  );
}

export function StartPageView({
  view,
  mobile,
  actions,
  firstRun,
  sessionEntry,
}: StartPageViewProps) {
  return (
    <section className="page page-start start-page" data-page="start">
      <PageHeader className="start-page__header" title="开始" />

      {view.status === 'loading' ? (
        <LoadingState
          description={
            mobile
              ? '正在通过授权主机读取真实目录…'
              : '正在连接当前 CLI 实例…'
          }
        />
      ) : null}

      {view.status === 'empty' ? (
        <EmptyState
          title={view.title ?? '暂无可显示的实时数据'}
          description={
            view.description ??
            '当前连接没有返回可展示的数据。'
          }
        />
      ) : null}

      {view.status === 'error' ? (
        <ErrorState
          message={view.message}
          code={view.code}
          retryable={view.retryable}
          retryLabel="重试本机请求"
          onRetry={actions.retry}
        />
      ) : null}

      {view.status === 'unsupported' ? (
        <UnsupportedState reason={view.reason} code={view.code} />
      ) : null}

      {view.status === 'ready' ? (
        <ReadyStartPage
          data={view.data}
          actions={actions}
          firstRun={firstRun}
          sessionEntry={sessionEntry}
        />
      ) : null}
    </section>
  );
}
