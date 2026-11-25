import type { LinkedSession, SessionAction } from './session/session-types';
import { EXTERNAL_INVITE_CODE, OWNER_INVITE_CODE } from './session/session-types';
import './session.css';

/**
 * Development-only session toolbar: identities, fixed invite codes and results.
 * It lives outside the product frame and never ships to the production build.
 */
export function SessionToolbar({
  session,
  onAction,
}: {
  readonly session: LinkedSession;
  readonly onAction: (action: SessionAction) => void;
}) {
  return (
    <div className="dev-session-toolbar" aria-label="会话工具条">
      <span className="dev-session-viewer">
        当前身份：{session.activeViewer === 'owner' ? '创建者' : '受邀成员'}
      </span>
      <button
        type="button"
        className="dev-session-button"
        onClick={() => onAction({ type: 'viewer-set', viewer: 'owner' })}
      >
        切换为创建者
      </button>
      <button
        type="button"
        className="dev-session-button"
        onClick={() => onAction({ type: 'viewer-set', viewer: 'invitee' })}
      >
        切换为受邀成员
      </button>
      <span className="dev-session-code">
        外部邀请码：{EXTERNAL_INVITE_CODE}
      </span>
      <span className="dev-session-code">
        创建者邀请码：{OWNER_INVITE_CODE}
      </span>
      <button
        type="button"
        className="dev-session-button"
        onClick={() => onAction({ type: 'machine-add' })}
      >
        加入第二台电脑
      </button>
      <button
        type="button"
        className="dev-session-button"
        onClick={() =>
          onAction({ type: 'communication-set', value: 'unavailable' })
        }
      >
        切换通信为不可用
      </button>
      <button
        type="button"
        className="dev-session-button"
        onClick={() =>
          onAction({ type: 'communication-set', value: 'available' })
        }
      >
        切换通信为可用
      </button>
      {session.lastResult ? (
        <span className="dev-session-result" role="status">
          {session.lastResult}
        </span>
      ) : null}
    </div>
  );
}
