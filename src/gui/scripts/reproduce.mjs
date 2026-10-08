import { cp, mkdir, mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const source = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const destination = await mkdtemp(join(tmpdir(), 'gui-deep-modules-repro-'));
const project = join(destination, 'project');
const home = join(destination, 'home');
const cache = join(destination, 'cache');
const scratch = join(destination, 'tmp');
await Promise.all([home, cache, scratch].map(path => mkdir(path)));
await cp(source, project, { recursive: true, filter: path => {
  const parts = path.slice(source.length).split(sep);
  return !parts.some(part => ['node_modules', '.cache', 'coverage', '.git'].includes(part)) && !path.endsWith('.log');
} });
const cacheFromIndex = process.argv.indexOf('--cache-from');
if (cacheFromIndex !== -1) {
  const cacheFrom = process.argv[cacheFromIndex + 1];
  if (!cacheFrom || !cacheFrom.startsWith('/')) throw new Error('--cache-from requires an explicit absolute test cache');
  // Only package tarballs/content-addressed cache, never login/npm configuration.
  await cp(join(cacheFrom, '_cacache'), join(cache, '_cacache'), { recursive: true });
}
const env = { PATH: process.env.PATH, HOME: home, TMPDIR: scratch, npm_config_cache: cache };
const results = [];
async function run(label, args) {
  const result = await new Promise((resolveRun, reject) => {
    const child = spawn('npm', args, { cwd: project, env, stdio: 'inherit' });
    child.on('error', reject);
    child.on('close', code => resolveRun({ label, code }));
  });
  results.push(result);
  if (result.code !== 0) throw new Error(`${label} failed with ${result.code}; retained ${destination}`);
}
const install = ['ci', '--ignore-scripts', '--no-audit', '--no-fund'];
if (cacheFromIndex !== -1) install.push('--offline');
await run('clean npm ci', install);
await run('complete verify', ['run', 'verify']);
const manifest = JSON.parse(await readFile(join(project, 'package.json'), 'utf8'));
console.log(JSON.stringify({ status: 'PASS', source, destination, package: manifest.name, results }, null, 2));
