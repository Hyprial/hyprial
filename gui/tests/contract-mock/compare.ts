/**
 * Reply comparison for the Contract Mock conformance tests.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Two forms agree when status, the envelope without volatile fields and the
 *   catalog-digest header are equal. `requestId` is minted per reply, so it is
 *   the only envelope field stripped by default; session payloads add the
 *   per-session secrets and the expiry.
 * - The session layer's own version declaration (`sessionContract`, contract
 *   section 13) is written by the Python host and not by the in-browser form,
 *   which has no host, so it is the one field of a session that only one form
 *   carries (2026-11-16, when the Node host that omitted it was deleted).
 * - The comparator is itself under test (altered result, altered status and a
 *   missing header must each be reported), so a comparator that cannot fail
 *   cannot vouch for the forms.
 */

import { deepEqual } from '../../contract-mock/index.ts';

export interface Observed {
  status: number;
  envelope: unknown;
  digest: string | null;
}

export const SESSION_VOLATILE = ['view', 'generation', 'csrf', 'expiresAt', 'requestId', 'sessionContract'] as const;

export function strip(value: unknown, keys: readonly string[] = ['requestId']): unknown {
  if (Array.isArray(value)) return value.map((item) => strip(item, keys));
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !keys.includes(key))
        .map(([key, item]) => [key, strip(item, keys)]),
    );
  }
  return value;
}

export async function observe(response: Response): Promise<Observed> {
  const text = await response.text();
  let envelope: unknown;
  try {
    envelope = JSON.parse(text);
  } catch {
    envelope = { raw: text };
  }
  return { status: response.status, envelope, digest: response.headers.get('x-hyprial-catalog-digest') };
}

export function differences(
  label: string,
  left: Observed,
  right: Observed,
  volatile: readonly string[] = ['requestId'],
): string[] {
  const found: string[] = [];
  if (left.status !== right.status) found.push(`${label}: status ${left.status} != ${right.status}`);
  if (left.digest !== right.digest) found.push(`${label}: digest header ${String(left.digest)} != ${String(right.digest)}`);
  if (!deepEqual(strip(left.envelope, volatile), strip(right.envelope, volatile))) {
    found.push(`${label}: envelope ${JSON.stringify(strip(left.envelope, volatile)).slice(0, 200)} != ${JSON.stringify(strip(right.envelope, volatile)).slice(0, 200)}`);
  }
  return found;
}
