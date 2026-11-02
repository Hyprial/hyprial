import type { Dispatch } from 'react';
import { Button } from '../../../components/primitives';
import type { AppAction } from '../../../state/types';
import { ORG_COPY } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import { findOrg } from '../domain/selectors';
import '../workspace/workspace.css';

/**
 * 发送被拒的提示（第 8 节，F18）：气泡下方显示原因与修复入口。只在后端返回被拒原因时出现；
 * 未接通时不显示。被拒时与聊天输入区的“重试”配合：先修复，再手动重试，不自动重发。
 * 观察期的消息已经送达，只提示强制期后的结果，不提示重发。
 */
export function ChatRefusalHint({
  messageId,
  targetId,
  dispatch,
}: {
  readonly messageId: string;
  readonly targetId: string;
  readonly dispatch: Dispatch<AppAction>;
}) {
  const domain = useOrgDomain();
  const refusal = domain.provided ? domain.snapshot.chatRefusals[messageId] : undefined;
  if (!refusal) {
    return null;
  }
  const org = findOrg(domain.snapshot, refusal.orgId);
  // “邀请 Y 加入组织”：只对所有者显示，邀请进我作为所有者的当前工作组织。
  const current = findOrg(domain.snapshot, domain.snapshot.currentOrgId);
  const inviteOrg = current?.kind === 'work' && current.role === 'owner' ? current : null;
  const text =
    refusal.code === 'NO_SHARED_ORG'
      ? ORG_COPY.noSharedOrg(refusal.peerLabel)
      : ORG_COPY.policyDenied(refusal.orgName ?? org?.name ?? ORG_COPY.orgUnknown);
  // 修复入口写出是哪个 Agent；组织数据里没有这个 Agent 时用通称。
  const agentName = Object.values(domain.snapshot.details)
    .flatMap((detail) => detail?.agents ?? [])
    .find((agent) => agent.id === targetId)?.name;

  return (
    <div
      className={refusal.observation ? 'org-refusal org-refusal--observation' : 'org-refusal'}
      data-dev-ui="DEV-UI-ORG-017"
      role="note"
    >
      <p className="org-refusal__text">
        {refusal.observation ? `${ORG_COPY.observationNote}${text}` : text}
      </p>
      <div className="org-actions">
        {refusal.code === 'NO_SHARED_ORG' ? (
          <>
            <Button
              size="sm"
              onClick={() => {
                domain.requestAgentView({
                  agentId: targetId,
                  section: 'orgs',
                  joinOrgId: refusal.orgId,
                });
                dispatch({ type: 'open-agent-detail', agentId: targetId });
              }}
            >
              {ORG_COPY.fixJoinOrg(refusal.peerLabel, agentName)}
            </Button>
            {inviteOrg ? (
              <Button
                size="sm"
                onClick={() => {
                  domain.requestOrgView({
                    tab: 'invites',
                    segment: 'send',
                    prefillAccount: refusal.inviteAccount,
                    orgId: inviteOrg.id,
                  });
                  dispatch({ type: 'navigate', page: 'org' });
                }}
              >
                {ORG_COPY.fixInvite(refusal.peerLabel)}
              </Button>
            ) : null}
          </>
        ) : (
          <Button
            size="sm"
            onClick={() => {
              domain.requestOrgView({ tab: 'settings', segment: 'comm', orgId: refusal.orgId });
              dispatch({ type: 'navigate', page: 'org' });
            }}
          >
            {ORG_COPY.fixPolicy(refusal.orgName ?? org?.name ?? ORG_COPY.orgUnknown)}
          </Button>
        )}
      </div>
      {refusal.observation ? null : <p className="org-muted">{ORG_COPY.refusalRetry}</p>}
    </div>
  );
}
