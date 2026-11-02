import { AgentAvatar } from '../../components/AgentAvatar';
import { AvailabilityBadge } from '../../components/Badge';
import { EmptyState, ErrorState, LoadingState } from '../../components/primitives';
import type { SessionTarget } from '../../state/gui-view-model';

/** Controlled projection of the targets.list directory. */
export function AgentTargetField({ targets, activeTargetId, onValueChange, state = 'ready', error, disabled = false }: {
  readonly targets: readonly SessionTarget[];
  readonly activeTargetId: string | null;
  readonly onValueChange: (id: string) => void;
  readonly state?: 'loading' | 'ready' | 'error';
  readonly error?: string;
  readonly disabled?: boolean;
}) {
  return <aside className="chat-workspace__targets" aria-label="聊天对象">
    <h2>聊天对象</h2>
    {state === 'loading' && targets.length === 0 ? <LoadingState title="正在加载聊天对象" /> :
      state === 'error' && targets.length === 0 ? <ErrorState message={error ?? '聊天对象目录不可用。'} /> :
      targets.length === 0 ? <EmptyState title="暂无聊天对象" /> :
      <div className="chat-workspace__target-list">
        {state === 'error' && <ErrorState message={error ?? '聊天对象目录不可用。'} />}
        {targets.map(target => <button key={target.id} data-target-id={target.id} type="button" className="chat-workspace__target"
          aria-pressed={target.id === activeTargetId} disabled={disabled} onClick={() => onValueChange(target.id)}>
          <AgentAvatar name={target.displayName} tone={target.avatarTone} />
          <span className="chat-workspace__target-copy"><strong>{target.displayName}</strong><span>{target.role}</span><AvailabilityBadge availability={target.availability} /></span>
        </button>)}
      </div>}
  </aside>;
}
