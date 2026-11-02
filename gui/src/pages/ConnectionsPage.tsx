import { useState } from 'react';
import type { Dispatch, ReactNode } from 'react';
import type { StubOperations } from '../adapters/stub';
import type { ConnectionSummary } from '../state/gui-view-model';
import { useStubAction } from '../adapters/useStubAction';
import {
  ProviderLoginFlow,
  type ProviderOption,
} from '../features/connections/ProviderLoginFlow';
import '../features/connections/provider-login.css';
import {
  Badge,
  ConnectionStatusBadge,
} from '../components/Badge';
import { Button, Card } from '../components/primitives';
import type { AppAction, AppState } from '../state/types';
import type { SessionAction } from '../dev-mode/session/session-types';
import type { ModelMethod, ModelServicesSectionModel } from '../features/connections/model-services-view-model';
import { ModelServiceSection } from '../features/connections/ModelServiceSection';
import type { MessageConnectionModel } from '../features/connections/message-connection-view-model';
import { MessageConnectionSection } from '../features/connections/MessageConnectionSection';

interface ConnectionsPageProps {
  readonly channelContent?: ReactNode;
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly stub: StubOperations;
  /** J10 model section; present only in the development session. */
  readonly model?: ModelServicesSectionModel;
  readonly onModelAction?: (action: SessionAction) => void;
  readonly message?: MessageConnectionModel;
  readonly onMessageAction?: (action: SessionAction) => void;
  readonly messageAgents?: readonly { readonly id: string; readonly name: string }[];
  readonly messageDefaultAgentId?: string | null;
}

const LOCAL_PROVIDER_PREFIX = 'provider-local-';

export function ConnectionsPage({
  state,
  dispatch,
  stub,
  channelContent,
  model,
  onModelAction,
  message,
  onMessageAction,
  messageAgents = [],
  messageDefaultAgentId = null,
}: ConnectionsPageProps) {
  const stubAction = useStubAction(stub, dispatch);
  const [loginOpen, setLoginOpen] = useState(false);
  // 页内内存中的开发态 provider 登录结果：不落存储、不发请求。
  const [localProviders, setLocalProviders] = useState<ConnectionSummary[]>([]);
  // 模型侧只有“已登录的 provider”：凭据属于账号（user/daemon provider 命令），
  // 不存在让用户填写的“主/备用模型服务”实体，未登录的不渲染成待配置卡片。
  const loggedInProviders = [
    ...state.connections.filter(
      (connection) => connection.kind === 'model' && connection.status === 'connected',
    ),
    ...localProviders,
  ];


  return (
    <section className="page page-connections" data-page="connections">
      <h1 className="page-title">连接</h1>
      <p className="page-subtitle">
        管理已登录的服务商（provider）与消息连接。
      </p>

      <div className="connections-grid">
        <ModelServiceSection
            disabled={!model}
            model={model ?? { services: [], addMode: 'ready' }}
            onAdd={(label: string, method: ModelMethod) => {
              onModelAction?.({ type: 'model-add', label, method });
              return !model || model.addMode === 'failure' ? 'failure' : 'success';
            }}
            onSetDefault={(serviceId: string) =>
              onModelAction?.({ type: 'model-set-default', serviceId })
            }
          />

        <Card
          as="section"
          padding="lg"
          className="card connection-card"
          aria-labelledby="models-title"
        >
          <div className="section-heading">
            <div>
              <h2 id="models-title">服务商</h2>
              <p>已登录的服务商（provider）；凭据属于你的账号，Agent 经授权使用</p>
            </div>
            <Button
              variant="secondary"
              className="button button-secondary"
              disabled={!!channelContent || stubAction.busy}
              onClick={() => setLoginOpen(true)}
            >
              登录服务商
            </Button>
          </div>

          {channelContent ? <p role="status">模型凭据状态尚未确认，配置暂不可用。</p> : (
          <div className="connection-list">
            {loggedInProviders.length === 0 && (
              <div className="empty-state connections-model-empty">
                <strong>尚未登录任何服务商</strong>
                <p>
                  服务商凭据登录后属于你的账号；未登录时 Agent 无法调用模型。
                </p>
              </div>
            )}
            {loggedInProviders.map((connection) => (
              <div
                key={connection.id}
                className="connection-row"
                data-testid={`connection-${connection.id}`}
              >
                <div className="connection-main">
                  <div className="connection-title-row">
                    <strong>{connection.label}</strong>
                    {connection.isDefault && (
                      <Badge tone="neutral">默认</Badge>
                    )}
                  </div>
                  <p className="connection-meta">
                    {connection.detail} · {connection.usedBy} 个 Agent 使用
                  </p>
                  <ConnectionStatusBadge status={connection.status} />
                </div>
                <div className="connection-actions">
                  {connection.status === 'connected' &&
                    !connection.isDefault && (
                      <Button
                        variant="secondary"
                        className="button button-secondary connection-button"
                        aria-label={`设为默认 ${connection.label}`}
                        disabled={stubAction.busy}
                        onClick={() =>
                          void stubAction.run(
                            'connection.model.setDefault',
                            { connectionId: connection.id },
                          )
                        }
                      >
                        设为默认
                      </Button>
                    )}
                  <Button
                    variant="secondary"
                    className="button button-secondary connection-button"
                    aria-label={`退出登录 ${connection.label}`}
                    disabled={
                      stubAction.busy &&
                      !connection.id.startsWith(LOCAL_PROVIDER_PREFIX)
                    }
                    onClick={() => {
                      if (connection.id.startsWith(LOCAL_PROVIDER_PREFIX)) {
                        // 开发态内存条目：本地移除即可，无网络事实。
                        setLocalProviders((current) =>
                          current.filter((item) => item.id !== connection.id),
                        );
                        return;
                      }
                      void stubAction.run('connection.model.logout', {
                        connectionId: connection.id,
                      });
                    }}
                  >
                    退出登录
                  </Button>
                </div>
              </div>
            ))}
          </div>
          )}
        </Card>

        {channelContent ?? <MessageConnectionSection
          model={message ?? { state: 'unavailable', boundAgentName: null, authorizeOutcome: 'failure' }}
          agentOptions={messageAgents} defaultAgentId={messageDefaultAgentId}
          onAuthorize={agentId => onMessageAction?.({ type: 'feishu-authorize', agentId })} />}

      </div>

      <div className="boundary-note" role="note">
        <strong>{channelContent ? '模型凭据状态尚未确认' : '连接状态尚未确认'}</strong>
        <p>{channelContent ? '模型凭据尚未接通，登录与默认切换暂不可用。' : '连接操作暂不可用。'}</p>
      </div>

      {loginOpen && (
        <ProviderLoginFlow
          onClose={() => setLoginOpen(false)}
          onComplete={(provider: ProviderOption) => {
            setLocalProviders((current) => [
              ...current.filter(
                (item) => item.id !== `${LOCAL_PROVIDER_PREFIX}${provider}`,
              ),
              {
                id: `${LOCAL_PROVIDER_PREFIX}${provider}`,
                kind: 'model',
                label: provider,
                detail: '已登录（内存数据，未接后端）',
                status: 'connected',
                usedBy: 0,
              },
            ]);
            setLoginOpen(false);
          }}
          dispatch={dispatch}
        />
      )}
    </section>
  );
}
