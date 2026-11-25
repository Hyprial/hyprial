import type { SessionTarget } from '../state/gui-view-model';

interface AgentAvatarProps {
  readonly name: string;
  readonly tone: SessionTarget['avatarTone'];
  readonly size?: 'sm' | 'md' | 'lg';
}

const SQUIRE_FACE = '(✧‿✧)';

const FACE_POOL: readonly string[] = [
  '(^‿^)',
  '{^‿^}',
  '<^‿^>',
  '[^‿^]',
  '(◠‿◠)',
  '{◠‿◠}',
  '[◠‿◠]',
];

function nameHash(name: string): number {
  let hash = 0;
  for (let index = 0; index < name.length; index += 1) {
    hash = (hash * 31 + name.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function faceFor(name: string): string {
  const key = name.trim().toLowerCase();
  if (key === 'squire') {
    return SQUIRE_FACE;
  }
  return FACE_POOL[nameHash(key) % FACE_POOL.length] ?? SQUIRE_FACE;
}

export function AgentAvatar({
  name,
  tone,
  size = 'md',
}: AgentAvatarProps) {
  return (
    <span
      className={`agent-avatar agent-avatar-${size} avatar-${tone}${name.trim().toLowerCase() === 'squire' ? ' agent-avatar--squire' : ''}`}
      aria-hidden="true"
    >
      <span className="agent-avatar__face">{faceFor(name)}</span>
    </span>
  );
}
