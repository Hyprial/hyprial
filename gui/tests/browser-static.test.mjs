import { describe, expect, it } from 'vitest';
import { isStaticFavicon } from './support/browser.mjs';
describe('browser traffic classification', () => {
  it.each([
    ['GET', 'http://127.0.0.1:1234/favicon.svg', true],
    ['POST', 'http://127.0.0.1:1234/favicon.svg', false],
    ['GET', 'http://127.0.0.1:1234/favicon.svg?token=x', false],
    ['GET', 'http://other.invalid/favicon.svg', false],
    ['GET', 'http://127.0.0.1:1234/daemon/session', false],
  ])('classifies only exact same-origin favicon: %s %s', (method, url, expected) => {
    expect(isStaticFavicon({ method: () => method, url: () => url }, 'http://127.0.0.1:1234')).toBe(expected);
  });
});
