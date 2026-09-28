#!/usr/bin/env node
/**
 * Seed the Hyprial API provider route into the DSH user settings layer.
 *
 * The route lands in `<DSH_HOME>/settings.yaml`, the layer DSH merges per
 * provider route on top of the composition base. It is deliberately not a
 * bundle patch: `dsh-app-boot` applies a patch row by assigning its whole
 * `config`, so a second bundle patching the shared `llm-pi-ai` row would drop
 * the providers other bundles declare there.
 *
 * The route declares no `models` list, because that list *replaces* the
 * served catalog rather than extending it, and the gateway authorizes models
 * per account group. The only correct list is the one its own endpoint
 * reports, which Settings -> Models fetches through "Fetch available models"
 * once the key is stored.
 *
 * No credential is written here: the route names the credential ref it needs,
 * and the user pastes the key once for DSH to store under that ref.
 */
import { existsSync, readFileSync, writeFileSync, mkdirSync, lstatSync, renameSync, rmSync, openSync, closeSync, realpathSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export const PROVIDER_ROUTE = 'hyprial';
export const HYPRIAL_CREDENTIAL_REF = 'HYPRIAL_API_KEY';
/** The `/v1` base every OpenAI-style request and the model listing join onto. */
export const HYPRIAL_API_BASE_URL = 'https://api.hyprial.ai/v1';
/**
 * The protocol this route speaks. A hand-declared route has no installed
 * catalog entry to supply an `api`, `baseURL` or per-model capacities, so the
 * route states them and every fetched model inherits them.
 */
export const HYPRIAL_PROVIDER_PROFILE = Object.freeze({
  displayName: 'Hyprial API',
  api: 'openai-completions',
  baseURL: HYPRIAL_API_BASE_URL,
  apiKeyEnv: HYPRIAL_CREDENTIAL_REF,
});

/**
 * Add the provider route only when no `hyprial` route exists yet, preserving
 * comments, unrelated namespaces, the other providers under `llm-pi-ai`, and
 * any user edit of this same route.
 * @returns whether the settings document changed, and its path.
 */
export async function seedHyprialProvider({ dshHome = resolve(process.env.DSH_HOME || join(homedir(), '.dsh')), providers = { [PROVIDER_ROUTE]: HYPRIAL_PROVIDER_PROFILE } } = {}) {
  // Editing user settings runs after dependencies exist rather than during the
  // dependency-free preflight, which must not touch the filesystem.
  const { parseDocument, isMap } = await import('yaml');
  const file = join(dshHome, 'settings.yaml');
  mkdirSync(dshHome, { recursive: true, mode: 0o700 });
  const lockPath = join(dshHome, '.hyprial-provider-seed.lock');
  let lock;
  try { lock = openSync(lockPath, 'wx', 0o600); }
  catch (error) { throw new Error('Cannot lock the DSH settings seed; another setup may be running', { cause: error }); }
  let temp;
  try {
    const existed = existsSync(file);
    const stat = existed ? lstatSync(file) : undefined;
    if (stat && (!stat.isFile() || stat.isSymbolicLink())) throw new Error('DSH settings must be a regular file; seed a linked settings file explicitly');
    const original = existed ? readFileSync(file, 'utf8') : '';
    const document = parseDocument(original);
    if (document.errors.length) throw new Error(`Invalid DSH settings: ${document.errors[0].message}`);
    if (document.contents !== null && !isMap(document.contents)) throw new Error('DSH settings must be a mapping');
    for (const path of [['llm-pi-ai'], ['llm-pi-ai', 'providers']]) {
      const value = document.getIn(path, true);
      if (value !== undefined && !isMap(value)) throw new Error(`${path.join('.')} must be a mapping`);
    }
    // A route the profile already declares — seeded earlier or written by hand
    // — is the user's to keep, including its models and its credential ref.
    let changed = false;
    for (const [route, defaults] of Object.entries(providers)) {
      if (document.hasIn(['llm-pi-ai', 'providers', route])) continue;
      document.setIn(['llm-pi-ai', 'providers', route], { ...defaults });
      changed = true;
    }
    if (!changed) return { changed: false, file };
    const updated = document.toString();
    temp = join(dshHome, `.settings.hyprial-provider-${randomUUID()}.tmp`);
    writeFileSync(temp, updated, { mode: stat ? stat.mode & 0o777 : 0o600, flag: 'wx' });
    if (existed && readFileSync(file, 'utf8') !== original) throw new Error('DSH settings changed during seeding; nothing was written, retry once the editor finishes');
    renameSync(temp, file);
    temp = undefined;
    return { changed: true, file };
  } finally {
    if (temp) rmSync(temp, { force: true });
    closeSync(lock);
    rmSync(lockPath, { force: true });
  }
}

if (process.argv[1] && existsSync(process.argv[1]) && realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1])) {
  try {
    const { changed, file } = await seedHyprialProvider();
    console.log(changed
      ? `Seeded the Hyprial API provider route in ${file}; paste your API key in Settings -> Models, then fetch its models.`
      : `The Hyprial API provider route is already declared in ${file}; settings were left unchanged.`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
