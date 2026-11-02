import { useRef } from 'react';
import { ORG_COPY, zh } from '../domain/copy';
import type { ReceivedInvite } from '../domain/types';
import { OrgDialog } from './OrgDialog';

/** 领取邀请的确认（P05）：写明加入后的影响；确认后在列表中逐条显示结果。 */
export function ClaimInviteDialog({
  invite,
  onClose,
  onConfirm,
}: {
  readonly invite: ReceivedInvite;
  readonly onClose: () => void;
  readonly onConfirm: () => void;
}) {
  const leadRef = useRef<HTMLParagraphElement>(null);
  return (
    <OrgDialog
      title={zh`加入 ${invite.orgName}？`}
      onClose={onClose}
      initialFocusRef={leadRef}
      devUi="DEV-UI-ORG-007"
      actions={[
        { label: '取消', onClick: onClose },
        { label: ORG_COPY.join, variant: 'primary', onClick: onConfirm },
      ]}
    >
      <p ref={leadRef} tabIndex={-1} className="org-dialog__lead">
        {ORG_COPY.claimImpact}
      </p>
      <dl className="org-facts">
        <div>
          <dt>邀请人</dt>
          <dd>{invite.inviterLabel}</dd>
        </div>
        <div>
          <dt>接受时间</dt>
          <dd>{invite.acceptedAt}</dd>
        </div>
      </dl>
    </OrgDialog>
  );
}
