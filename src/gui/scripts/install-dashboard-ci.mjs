import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dashboard = resolve(dirname(fileURLToPath(import.meta.url)), '../dashboard');

// npm can report success after failing to install a platform optional package.
// Probe in a fresh process so a failed import is not cached across attempts.
export function installDashboard({ run = spawnSync, log = console.error } = {}) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const install = run('npm', ['ci', '--include=dev', '--include=optional',
      '--registry=https://registry.npmjs.org', '--fetch-retries=1', '--fetch-timeout=60000',
      '--no-audit', '--no-fund'], { cwd: dashboard, stdio: 'inherit', timeout: 240000 });
    if (install.error || install.status !== 0) throw new Error('Locked Dashboard dependency installation failed');
    const probe = run(process.execPath, ['--input-type=module', '-e', 'await import("rolldown")'],
      { cwd: dashboard, encoding: 'utf8', timeout: 30000 });
    if (!probe.error && probe.status === 0) return;
    const details = String(probe.stderr || '');
    const missingBinding = /Cannot find native binding/.test(details)
      && /Cannot find module ['"](?:@rolldown\/binding-|\.\/rolldown-binding\.)/.test(details);
    if (attempt === 1 && !probe.error && missingBinding) {
      log(`Dashboard native binding missing on ${process.platform}/${process.arch}; retrying locked installation once.`);
      continue;
    }
    log(details);
    throw new Error('Dashboard native binding verification failed');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) installDashboard();
