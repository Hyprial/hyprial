// Human Console management only. Never expose these handlers as model tools or
// route credential-bearing inputs through the generic audited Shell service.
import { createOrgCliRunner, orgAcceptedPath } from './org-cli-runner.mjs';
export const MANAGEMENT_OPERATIONS = Object.freeze([
  'adapter-enroll-preview', 'adapter-enroll', 'adapter-authorize',
  'org-management-status', 'org-fetch', 'org-import-preview', 'org-import'
]);

export function assertManagementRequest(req) {
  const headers = req.headers || {};
  let origin;
  try { if (typeof headers.origin === 'string') origin = new URL(headers.origin); } catch {}
  if (req.method !== 'POST' || !origin || origin.origin !== headers.origin || !['http:', 'https:'].includes(origin.protocol) || origin.host !== headers.host ||
      headers['sec-fetch-site'] === 'cross-site' ||
      !/^application\/json(?:\s*;|$)/i.test(headers['content-type'] || '')) {
    throw Object.assign(new Error('Console management requires a same-origin JSON request'), { code: 'ORIGIN_DENIED' });
  }
}

export function createConsoleManagement({ requireCapability, readOnlyStatus, env = process.env }) {
  const hostEnv = { ...env };
  let adapter, organization;
  return async function handle(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input) || !MANAGEMENT_OPERATIONS.includes(input.operation)) {
      throw Object.assign(new Error('Unsupported Console management operation'), { code: 'UNSUPPORTED_OPERATION' });
    }
    await requireCapability(input.operation);
    if (input.operation.startsWith('adapter-')) {
      adapter ||= import('./adapter-enrollment.mjs').then(({ createAdapterEnrollment }) => createAdapterEnrollment({ env: hostEnv }));
      return (await adapter).handle(input);
    }
    organization ||= import('./org-adoption.mjs').then(async ({ createOrgAdoptionController }) => createOrgAdoptionController({
      runCli: createOrgCliRunner({ env: hostEnv }),
      readOnlyStatus,
      acceptedPath: await orgAcceptedPath(hostEnv)
    }));
    return (await organization)(input);
  };
}
