/** The existing first-run public-code format; raw messages never become codes. */
export function publicErrorCode(value: unknown): string | undefined {
  return typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : undefined;
}
