#!/usr/bin/env node
import { migrateLegacyState, recoverPinnedBindings } from '../h2b-session-bridge.mjs';

try {
  const [action, ...args] = process.argv.slice(2);
  if (!['migrate', 'recover'].includes(action)) throw new Error('usage: node scripts/maintain-session-state.mjs migrate|recover [--apply] [--legacy-file /absolute/path]');
  const options = { apply: false };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--apply') options.apply = true;
    else if (args[i] === '--legacy-file' && action === 'migrate' && args[i + 1]?.startsWith('/')) options.legacyFile = args[++i];
    else throw new Error(`unsupported maintenance argument: ${args[i]}`);
  }
  const result = action === 'migrate'
    ? await migrateLegacyState(process.env, options)
    : await recoverPinnedBindings(process.env, options);
  process.stdout.write(JSON.stringify(result) + '\n');
  if (!result.ok) process.exitCode = 1;
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, code: error.code || 'STATE_MAINTENANCE_FAILED', error: error.message, details: error.details }) + '\n');
  process.exitCode = 1;
}
