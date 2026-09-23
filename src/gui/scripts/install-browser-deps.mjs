import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const browserDependencies = resolve(dirname(fileURLToPath(import.meta.url)), '../browser-tests');

// npm can report success after failing to install a platform optional package.
// Probe in a fresh process so a failed import is not cached across attempts.
export function installBrowserDependencies({ run = spawnSync, log = console.error } = {}) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const install = run('npm', ['ci', '--include=dev', '--include=optional',
      '--registry=https://registry.npmjs.org', '--fetch-retries=1', '--fetch-timeout=60000',
      '--no-audit', '--no-fund'], { cwd: browserDependencies, stdio: 'inherit', timeout: 240000 });
    if (install.error || install.status !== 0) throw new Error('Locked GUI browser dependency installation failed');
    const probe = run(process.execPath, ['--input-type=module', '-e', 'await import("rolldown")'],
      { cwd: browserDependencies, encoding: 'utf8', timeout: 30000 });
    if (!probe.error && probe.status === 0) return;
    const details = String(probe.stderr || '');
    const missingBinding = /Cannot find native binding/.test(details)
      && /Cannot find module ['"](?:@rolldown\/binding-|\.\/rolldown-binding\.)/.test(details);
    if (attempt === 1 && !probe.error && missingBinding) {
      log(`GUI browser native binding missing on ${process.platform}/${process.arch}; retrying locked installation once.`);
      continue;
    }
    log(details);
    throw new Error('GUI browser native binding verification failed');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) installBrowserDependencies();
