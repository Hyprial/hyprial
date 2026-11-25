import { isCanonicalAgentUri } from './daemon-api';
import type { QueryEntry } from './message-read-api';
import { agentTargetDisplayName } from '../../agent-chat';

export interface SendDirectoryTarget {
  readonly targetKind: string;
  readonly targetUri: string;
  readonly displayName?: unknown;
  readonly deliverable: boolean;
}

export interface SendSession {
  readonly conversationId: string;
  readonly targetUri: string;
  readonly displayName: string;
}

/** Only a directory-confirmed, unambiguous agent in an existing conversation can be selected. */
export function sendSessionsFromMessages(
  entries: readonly QueryEntry[],
  targets: readonly SendDirectoryTarget[],
): readonly SendSession[] {
  const available = new Map(targets.filter(row => row.targetKind === 'agent' && row.deliverable &&
    isCanonicalAgentUri(row.targetUri)).map(row => [row.targetUri, row]));
  const grouped = new Map<string, { agents: Set<string>; latest: number }>();
  for (const entry of entries) {
    if (typeof entry.conversationId !== 'string' || !entry.conversationId || /\s/.test(entry.conversationId)) continue;
    const agents = [entry.sender, entry.recipient, entry.from, entry.to].filter(isCanonicalAgentUri);
    const state = grouped.get(entry.conversationId) ?? { agents: new Set<string>(), latest: 0 };
    for (const agent of agents) state.agents.add(agent);
    state.latest = Math.max(state.latest, typeof entry.createdAtMs === 'number' ? entry.createdAtMs : 0);
    grouped.set(entry.conversationId, state);
  }
  return [...grouped.entries()]
    .filter(([, state]) => state.agents.size === 1 && available.has([...state.agents][0]))
    .sort((left, right) => right[1].latest - left[1].latest)
    .map(([conversationId, state]) => {
      const targetUri = [...state.agents][0];
      const target = available.get(targetUri)!;
      return { conversationId, targetUri, displayName: agentTargetDisplayName(target) };
    });
}
