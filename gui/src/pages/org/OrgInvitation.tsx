import { useState } from 'react';
import type { OrgCopyHandler, OrgInvitationState } from './org-view-model';

const COPY_SUCCESS: Readonly<Record<'link' | 'code', string>> = {
  link: '已复制邀请链接',
  code: '已复制邀请码',
};

const COPY_FAILURE = '复制失败，请手动选择文本';

export function OrgInvitation({
  invitation,
  onCopy,
}: {
  readonly invitation: OrgInvitationState;
  readonly onCopy?: OrgCopyHandler;
}) {
  const [message, setMessage] = useState('');

  if (invitation.state !== 'pending') {
    return null;
  }

  const text = (kind: 'link' | 'code'): string =>
    (kind === 'link' ? invitation.link : invitation.code) ?? '';

  async function copy(kind: 'link' | 'code'): Promise<void> {
    let ok = false;
    try {
      ok = onCopy ? await onCopy(kind, text(kind)) : false;
    } catch {
      ok = false;
    }
    setMessage(ok ? COPY_SUCCESS[kind] : COPY_FAILURE);
  }

  return (
    <section className="card org-block org-invitation" aria-labelledby="org-invitation">
      <h2 id="org-invitation">邀请状态</h2>
      <p className="org-invitation-row">
        <span className="badge badge-warning">{invitation.label}</span>
        {invitation.note ? (
          <span className="org-invitation-note">{invitation.note}</span>
        ) : null}
      </p>
      <div className="org-invitation-copy">
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void copy('link')}
        >
          复制邀请链接
        </button>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => void copy('code')}
        >
          复制邀请码
        </button>
      </div>
      {message ? (
        <p className="org-invitation-message" role="status">
          {message}
        </p>
      ) : null}
    </section>
  );
}
