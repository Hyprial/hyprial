/**
 * Label of one agent target (UUID addressing).
 *
 * Owner ruling 2026-11-07: a non-empty registry `displayName`, otherwise the
 * full agent URI. No fallback: nothing is derived from the actor or a URI
 * segment, and no placeholder text stands in for a missing name.
 */

export interface AgentTargetLabelSource {
  readonly targetUri: string;
  readonly displayName?: unknown;
}

export interface AgentUriLabelSource {
  readonly uri: string;
  readonly displayName?: unknown;
}

export function agentTargetDisplayName(row: AgentTargetLabelSource): string {
  return typeof row.displayName === 'string' && row.displayName.trim().length > 0 ? row.displayName : row.targetUri;
}

export function agentUriDisplayName(
  rows: readonly AgentUriLabelSource[],
  targetUri: string,
): string {
  const row = rows.find(candidate => candidate.uri === targetUri);
  return agentTargetDisplayName({ targetUri, displayName: row?.displayName });
}
