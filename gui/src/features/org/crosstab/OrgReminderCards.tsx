import { useState, type Dispatch } from 'react';
import { Button, Card } from '../../../components/primitives';
import type { AppAction } from '../../../state/types';
import { ORG_COPY } from '../domain/copy';
import { useOrgDomain } from '../domain/context';
import { pendingInvites } from '../domain/selectors';
import type { ReceivedInvite } from '../domain/types';
import { OrgDialog } from '../dialogs/OrgDialog';
import { resultMessage } from '../workspace/api';
import { MetaParts } from '../workspace/controls';
import { orgDataReady } from './agent-org';
import '../workspace/workspace.css';

/**
 * 开始页的组织提醒（第 9 节）：待处理邀请、迁入请求。数字只来自后端，
 * 没有数据时不显示卡片，不用 0 冒充“已确认没有”。
 */
export function OrgReminderCards({ dispatch }: { readonly dispatch: Dispatch<AppAction> }) {
  const domain = useOrgDomain();
  const { snapshot } = domain;
  if (!orgDataReady(domain.provided, snapshot)) {
    return null;
  }
  const invites = pendingInvites(snapshot)?.length ?? 0;
  const inbound =
    snapshot.inboundMigrations?.filter((ticket) => ticket.status === 'awaiting-target').length ?? 0;
  if (invites === 0 && inbound === 0) {
    return null;
  }
  return (
    <div className="org-reminders" data-dev-ui="DEV-UI-ORG-018" aria-label="组织提醒">
      {invites > 0 ? (
        <Card as="section" padding="md" className="org-reminder" aria-label="组织邀请提醒">
          <p className="org-reminder__title">{ORG_COPY.inviteReminder(invites)}</p>
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              domain.requestOrgView({ tab: 'invites', segment: 'received' });
              dispatch({ type: 'navigate', page: 'org' });
            }}
          >
            查看邀请
          </Button>
        </Card>
      ) : null}
      {inbound > 0 ? (
        <Card as="section" padding="md" className="org-reminder" aria-label="迁入请求提醒">
          <p className="org-reminder__title">{ORG_COPY.inboundReminder(inbound)}</p>
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              domain.requestOrgView({ tab: 'machines', panel: 'this-computer' });
              dispatch({ type: 'navigate', page: 'org' });
            }}
          >
            查看迁入请求
          </Button>
        </Card>
      ) : null}
    </div>
  );
}

type RowState = { readonly kind: 'pending' } | { readonly kind: 'done'; readonly text: string };

/**
 * 登录完成后的邀请确认（第 9 节、#2078 D6）：逐条列出，逐条“加入”，可以“稍后处理”。
 * 不静默全部接受；稍后处理后从提醒卡进入。每次打开 GUI 只提示一次（记在组织数据源上）。
 */
export function PostLoginInvites() {
  const domain = useOrgDomain();
  const [open, setOpen] = useState(
    () =>
      !domain.postLoginHandled &&
      orgDataReady(domain.provided, domain.snapshot) &&
      (pendingInvites(domain.snapshot)?.length ?? 0) > 0,
  );
  const [rows, setRows] = useState<Readonly<Record<string, RowState>>>({});
  if (!open) {
    return null;
  }
  const invites = pendingInvites(domain.snapshot) ?? [];

  function close(): void {
    domain.markPostLoginHandled();
    setOpen(false);
  }

  async function join(invite: ReceivedInvite): Promise<void> {
    setRows((prev) => ({ ...prev, [invite.id]: { kind: 'pending' } }));
    const result = await domain.gateway.claimInvite(invite.id);
    const text =
      result.kind === 'ok'
        ? ORG_COPY.joined(result.value.name)
        : result.kind === 'skipped'
          ? ORG_COPY.skipped(result.message)
          : ORG_COPY.claimFailed(invite.orgName, resultMessage(result) ?? ORG_COPY.unknown);
    setRows((prev) => ({ ...prev, [invite.id]: { kind: 'done', text } }));
  }

  return (
    <OrgDialog
      title="你有待处理的组织邀请"
      description={ORG_COPY.claimImpact}
      onClose={close}
      devUi="DEV-UI-ORG-018"
      actions={[
        // 全部处理完后不再是“稍后处理”。
        { label: invites.every((invite) => rows[invite.id]?.kind === 'done') ? '关闭' : '稍后处理', onClick: close },
      ]}
    >
      <ul className="org-rows" aria-label="待处理邀请">
        {invites.map((invite) => {
          const row = rows[invite.id];
          return (
            <li key={invite.id} className="org-row">
              <div className="org-row__main org-row__main--static">
                <span className="org-row__title">{invite.orgName}</span>
                <span className="org-row__meta">
                  <MetaParts parts={[`邀请人 ${invite.inviterLabel}`, `网页上已接受 ${invite.acceptedAt}`]} />
                </span>
                {row ? (
                  <span className="org-row__result" role="status">
                    {row.kind === 'pending' ? ORG_COPY.claiming(invite.orgName) : row.text}
                  </span>
                ) : null}
              </div>
              {!row ? (
                <Button variant="primary" onClick={() => void join(invite)}>
                  {ORG_COPY.join}
                </Button>
              ) : null}
            </li>
          );
        })}
      </ul>
    </OrgDialog>
  );
}
