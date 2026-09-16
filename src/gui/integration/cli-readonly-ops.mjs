// This map is intentionally a small implemented surface, not a copy of the CLI
// registry. The Host must still check the installed CLI's command capabilities.
export const READONLY_CLI_OPERATIONS = Object.freeze({
  'dispatch-matrix': Object.freeze(['dispatch', 'matrix']),
  'profile-list': Object.freeze(['profile', 'list']),
  'org-show': Object.freeze(['org', 'show'])
});

function invalid(message, code = 'INVALID_ARGUMENT') {
  const error = new Error(message);
  error.code = code;
  throw error;
}

// Return fixed argv for the existing bounded control-bridge runner. Do not
// accept executable, env, H2B_HOME, probe, shell, or arbitrary CLI arguments.
export function buildReadonlyOpsArgv(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    invalid('read-only operation must be a JSON object');
  }
  if (!Object.hasOwn(READONLY_CLI_OPERATIONS, input.operation)) {
    invalid('unsupported read-only operation', 'UNSUPPORTED_OPERATION');
  }
  const allowed = input.operation === 'dispatch-matrix'
    ? new Set(['operation', 'tier']) : new Set(['operation']);
  for (const key of Object.keys(input)) {
    if (!allowed.has(key)) invalid('unsupported field: ' + key);
  }
  const argv = [...READONLY_CLI_OPERATIONS[input.operation]];
  if (Object.hasOwn(input, 'tier')) {
    if (typeof input.tier !== 'string' || !['fast', 'strong', 'super'].includes(input.tier)) {
      invalid('tier must be fast, strong, or super; omit it to show all tiers');
    }
    argv.push('--tier', input.tier);
  }
  return [...argv, '--json'];
}

// Dependency injection keeps execution with the existing Host/bridge runner:
// its timeout, output cap, structured-error parsing and service identity remain
// authoritative. This helper never launches a second execution mechanism.
export async function executeReadonlyOps(input, runCli) {
  const argv = buildReadonlyOpsArgv(input);
  if (typeof runCli !== 'function') invalid('bounded CLI runner is required');
  return await runCli(argv);
}
