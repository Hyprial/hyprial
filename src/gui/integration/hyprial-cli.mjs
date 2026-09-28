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

// ctx.shell overlays env on the Host environment; omissions cannot remove old
// names. Supplying their current counterparts satisfies the CLI guard without
// forwarding unrelated Host secrets in each request.
export function hyprialShellRequest(request, source = process.env) {
  if (!/^hyprial\s/.test(request.command || '')) return request;
  const translated = hyprialCliEnv(source);
  const env = {};
  for (const name of Object.keys(source)) {
    if (name.startsWith('H2B_')) {
      const current = 'HYPRIAL_' + name.slice(4);
      env[current] = translated[current];
    }
  }
  return { ...request, env: { ...env, ...request.env } };
}
