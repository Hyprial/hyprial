import { useState } from 'react';
import type { ManagedAgentView } from '../../state/agent-selectors';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/primitives';
import {
  AGENT_GRANT_EMPTY_NOTE,
  AGENT_GRANT_STATE_LABELS,
  AGENT_RUNTIME_LABELS,
  capabilityGrantListHint,
  grantTone,
  providerAllowHint,
  providerGrantSummary,
  runtimeTone,
  type AgentExtensionView,
} from './agent-extensions';

/**
 * 「管理 Agent」panel: runtime, provider-key grants and capability grants.
 *
 * The two connection rows keep reading the shared selectors (model and
 * channel). Every action here is a placeholder: without a wired backend it
 * reports 暂不可用 and never success, and no request leaves the page.
 */

const RUNTIME_ACTIONS_UNAVAILABLE = {
  start: 'Agent 启动暂不可用。',
  stop: 'Agent 停止暂不可用。',
} as const;

const GRANT_ACTIONS_UNAVAILABLE = {
  allow: '服务商凭据授权暂不可用。',
  revoke: '撤销服务商授权暂不可用。',
} as const;

export interface AgentManagePanelProps {
  readonly agent: ManagedAgentView;
  readonly extension: AgentExtensionView;
  readonly onNavigateConnections: () => void;
}

export function AgentManagePanel({
  agent,
  extension,
  onNavigateConnections,
}: AgentManagePanelProps) {
  const [runtimeMessage, setRuntimeMessage] = useState<string | null>(null);
  const [grantMessage, setGrantMessage] = useState<string | null>(null);

  const grants = extension.providerGrants;
  const needsKey =
    grants.length === 0 ||
    grants.some(
      (grant) => grant.state === 'missing' || grant.state === 'unknown',
    );

  return (
    <div className="agent-manage" data-agent-id={agent.id}>
      <section className="agent-manage-section" aria-labelledby="agent-runtime-title">
        <h3 id="agent-runtime-title">运行</h3>
        <div className="agent-runtime-row">
          <Badge tone={runtimeTone(extension.runtime)}>
            {AGENT_RUNTIME_LABELS[extension.runtime]}
          </Badge>
          <Button
            variant="secondary"
            className="button button-secondary"
            onClick={() => setRuntimeMessage(RUNTIME_ACTIONS_UNAVAILABLE.start)}
          >
            启动
          </Button>
          <Button
            variant="secondary"
            className="button button-secondary"
            onClick={() => setRuntimeMessage(RUNTIME_ACTIONS_UNAVAILABLE.stop)}
          >
            停止
          </Button>
        </div>
        {/* 运行时事实与「首选 harness」偏好分离：启动后的实际值来自运行态，
            无数据源时诚实显示状态未确认。 */}
        <dl className="agent-runtime-facts">
          <div>
            <dt>实际 harness</dt>
            <dd data-runtime="harness">{extension.harness ?? '状态未确认'}</dd>
          </div>
          <div>
            <dt>正在使用的模型</dt>
            <dd data-runtime="model">{extension.model ?? '状态未确认'}</dd>
          </div>
        </dl>
        {runtimeMessage && (
          <p className="agent-placeholder-note" role="status">
            {runtimeMessage}
          </p>
        )}
      </section>

      <section
        className="agent-manage-section"
        aria-labelledby="agent-credential-title"
      >
        <h3 id="agent-credential-title">模型凭据</h3>
        <p className="agent-manage-note">
          秘钥属于授权人并按其账号计费；Agent 只持有授权，可单独撤销。界面不显示秘钥内容。
          本节为服务商（provider）凭据授权；Agent 自有的服务秘钥（agent
          secret）另行管理，不在此显示。
        </p>

        {grants.length === 0 ? (
          <p className="agent-manage-note" role="status">
            服务商凭据授权状态未确认。
          </p>
        ) : (
          <ul className="agent-grant-list">
            {grants.map((grant) => (
              <li key={`${grant.provider}-${grant.keyTail ?? ''}`} className="agent-grant-item">
                <Badge tone={grantTone(grant.state)}>
                  {AGENT_GRANT_STATE_LABELS[grant.state]}
                </Badge>
                <span className="agent-grant-summary">
                  {providerGrantSummary(grant)}
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="agent-credential-actions">
          <Button
            variant="secondary"
            className="button button-secondary"
            onClick={() => setGrantMessage(GRANT_ACTIONS_UNAVAILABLE.allow)}
          >
            授权使用我的服务商秘钥
          </Button>
          <Button
            variant="secondary"
            className="button button-secondary"
            onClick={() => setGrantMessage(GRANT_ACTIONS_UNAVAILABLE.revoke)}
          >
            撤销授权
          </Button>
        </div>
        {grantMessage && (
          <p className="agent-placeholder-note" role="status">
            {grantMessage}
          </p>
        )}

        {needsKey && (
          <div className="agent-credential-guide">
            <Button
              variant="primary"
              className="button button-strong"
              onClick={onNavigateConnections}
            >
              去连接页登录服务商
            </Button>
            <p className="agent-manage-note">
              账号已有秘钥时，也可直接授权给该 Agent：
            </p>
            <code className="agent-cli-hint">{providerAllowHint(agent.id)}</code>
          </div>
        )}
      </section>

      <section
        className="agent-manage-section"
        aria-labelledby="agent-grants-title"
      >
        <h3 id="agent-grants-title">授权</h3>
        <p className="agent-manage-note">
          能力授权（capability grant）的只读视图，记录与管理经 CLI。
        </p>
        {extension.capabilityGrants.length === 0 ? (
          <p className="agent-manage-note" role="status">
            {AGENT_GRANT_EMPTY_NOTE}
          </p>
        ) : (
          <ul className="agent-grant-list">
            {extension.capabilityGrants.map((grant) => (
              <li key={grant.id} className="agent-grant-item">
                <Badge
                  tone={
                    grant.state === 'granted'
                      ? 'positive'
                      : grant.state === 'revoked'
                        ? 'muted'
                        : 'neutral'
                  }
                >
                  {grant.state === 'granted'
                    ? '已授权'
                    : grant.state === 'revoked'
                      ? '已撤销'
                      : '状态未确认'}
                </Badge>
                <span className="agent-grant-summary">{grant.label}</span>
                <span className="agent-grant-id">{grant.id}</span>
              </li>
            ))}
          </ul>
        )}
        <code className="agent-cli-hint">{capabilityGrantListHint(agent.id)}</code>
      </section>
    </div>
  );
}
