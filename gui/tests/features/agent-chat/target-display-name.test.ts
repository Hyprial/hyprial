import { describe, expect, it } from 'vitest';
import { agentTargetDisplayName } from '../../../src/features/agent-chat';

const ID_FORM = 'agent:owner:node:a.abcdefghijklmnopqrstuvwxyz';
const NAME_FORM = 'agent:owner:node:worker';

describe('agent target labels', () => {
  it('shows a non-empty displayName when present', () => {
    expect(agentTargetDisplayName({ targetUri: ID_FORM, displayName: 'Research Bot' })).toBe('Research Bot');
    expect(agentTargetDisplayName({ targetUri: NAME_FORM, displayName: 'Research Bot' })).toBe('Research Bot');
  });

  it('shows the full URI, name-form or id-form, when displayName is absent', () => {
    expect(agentTargetDisplayName({ targetUri: ID_FORM })).toBe(ID_FORM);
    expect(agentTargetDisplayName({ targetUri: NAME_FORM })).toBe(NAME_FORM);
    for (const displayName of [undefined, null, '', '   ', 42]) {
      expect(agentTargetDisplayName({ targetUri: ID_FORM, displayName })).toBe(ID_FORM);
    }
  });

  it('derives nothing from the actor or a URI segment', () => {
    const row = { targetUri: ID_FORM, actor: 'agent:owner:node:worker' };
    expect(agentTargetDisplayName(row)).toBe(ID_FORM);
    expect(agentTargetDisplayName({ targetUri: NAME_FORM, actor: 'worker' } as never)).toBe(NAME_FORM);
  });
});
