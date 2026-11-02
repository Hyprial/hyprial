const code = value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : 'AUTH_FAILED';
const safeEventCode = value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : undefined;
const hasControlCharacter = value => [...value].some(char => {
  const point = char.codePointAt(0);
  return point <= 0x1f || point === 0x7f;
});
const text = (value, max = 256) => typeof value === 'string' && value.length <= max && !hasControlCharacter(value) ? value : undefined;
const httpsHost = value => {
  if (typeof value !== 'string' || value.length > 2048) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.host : undefined;
  } catch { return undefined; }
};
const AUTHORIZATION_EVENTS = new Set(['device', 'binding-device']);
const KNOWN_EVENTS = new Set([...AUTHORIZATION_EVENTS, 'authenticated', 'binding-proof-failed', 'committed']);
export function publicState(state) {
  if (!state || typeof state !== 'object') return undefined;
  return {
    authenticated: state.authenticated === true,
    owner: text(state.owner) ?? null,
    deviceReady: state.deviceReady === true,
    sidecarError: state.sidecarError ? code(state.sidecarError) : null,
    ready: state.ready === true,
  };
}
export function publicProgress(event) {
  if (!event || typeof event !== 'object' || typeof event.event !== 'string') return null;
  // Unknown event names are reduced to a stable marker: the name, raw message
  // and provider payload never cross the local middleware boundary.
  if (!KNOWN_EVENTS.has(event.event)) return { event: 'unknown' };
  const result = { event: event.event };
  if (AUTHORIZATION_EVENTS.has(event.event)) {
    const trustedHost = httpsHost(event.issuer);
    try {
      const url = new URL(event.verificationUri);
      // The local host only renders/opens a provider URL on the configured
      // profile issuer's host; an arbitrary https URL is still untrusted.
      if (
        trustedHost &&
        url.protocol === 'https:' &&
        !url.username &&
        !url.password &&
        url.host === trustedHost &&
        url.href.length <= 2048
      ) result.verificationUri = url.href;
    } catch { /* malformed provider URL is never forwarded */ }
    if (text(event.userCode, 128)) result.userCode = event.userCode;
    if (Number.isFinite(event.expiresIn) && event.expiresIn > 0) result.expiresIn = Math.min(event.expiresIn, 3600);
    if (Number.isFinite(event.interval) && event.interval > 0) result.interval = Math.min(event.interval, 300);
    if (event.event === 'binding-device' && typeof event.restarted === 'boolean') result.restarted = event.restarted;
  }
  if (event.event === 'binding-proof-failed') {
    const failureCode = safeEventCode(event.code);
    if (failureCode) result.code = failureCode;
  }
  if (text(event.owner)) result.owner = event.owner;
  return result;
}
export function publicResult(result) {
  const output = { ok: result?.ok === true };
  const state = publicState(result?.state);
  if (state) output.state = state;
  if (result?.code) output.code = code(result.code);
  if (result?.device) {
    output.device = { ready: result.device.ready === true };
    if (result.device.error) output.device.error = { code: code(result.device.error.code) };
  }
  return output;
}
