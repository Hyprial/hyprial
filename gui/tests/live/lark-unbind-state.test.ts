import { describe, expect, it } from 'vitest';
import { reconcileLarkUnbind } from '../../src/adapters/lark-unbind-state';

describe('Lark unbind reconciliation', () => {
  it.each([
    [null, 'success'],
    ['agent:owner:node:worker', 'unchanged'],
    ['agent:owner:node:other', 'changed'],
  ] as const)('classifies authoritative actor %s as %s', (actor, status) => {
    expect(reconcileLarkUnbind('agent:owner:node:worker', actor)).toBe(status);
  });
});
