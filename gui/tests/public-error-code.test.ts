import { describe, expect, it } from 'vitest';
import { publicErrorCode } from '../src/platform/shared/public-error-code';

describe('first-run public error code format', () => {
  it.each(['A', 'AUTH_BUSY', 'SETUP_RESULT_UNKNOWN', `A${'0'.repeat(79)}`])('preserves valid code %s', code => {
    expect(publicErrorCode(code)).toBe(code);
  });
  it.each([undefined, null, 42, {}, ['AUTH_BUSY'], '', '_AUTH', 'auth_busy', 'AUTH busy', 'AUTH.BUSY', `A${'0'.repeat(80)}`])('does not turn invalid input into a public code: %j', value => {
    expect(publicErrorCode(value)).toBeUndefined();
  });
});
