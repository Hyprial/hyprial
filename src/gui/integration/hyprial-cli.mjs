import { homedir } from 'node:os';
import { join } from 'node:path';

// Trusted Host environment only. Keep transitional GUI variables out of the
// public CLI process; never mutate the Host or accept environment from RPC input.
export function hyprialCliEnv(source = process.env) {
  const env = { ...source };
  for (const name of Object.keys(source)) {
    const current = name.startsWith('H2B_') ? 'HYPRIAL_' + name.slice(4)
      : name.startsWith('DSH_H2B_') ? 'DSH_HYPRIAL_' + name.slice(8) : null;
    if (!current) continue;
    if (!Object.hasOwn(source, current)) env[current] = source[name];
    delete env[name];
  }
  if (!Object.hasOwn(env, 'HYPRIAL_HOME')) env.HYPRIAL_HOME = join(env.HOME || homedir(), '.hyprial');
  return env;
}
