import type { SessionTarget } from '../state/gui-view-model';

interface AgentAvatarProps {
  readonly name: string;
  readonly tone: SessionTarget['avatarTone'];
  readonly size?: 'sm' | 'md' | 'lg';
}

export function AgentAvatar({
  name,
  tone,
  size = 'md',
}: AgentAvatarProps) {
  return (
    <span
      className={`agent-avatar agent-avatar-${size} avatar-${tone}`}
      aria-hidden="true"
    >
      {name.slice(0, 1)}
    </span>
  );
}
