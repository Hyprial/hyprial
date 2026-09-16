#!/usr/bin/env node
// Keep the managed process alive and hand H2B the URL issued by DSH itself.
import { spawn } from 'node:child_process';
import { writeFileSync, renameSync, unlinkSync } from 'node:fs';
import { isAbsolute } from 'node:path';
const file = process.env.HYPRIAL_GUI_LAUNCH_INFO_FILE || process.env.H2B_GUI_LAUNCH_INFO_FILE;
const expected = new URL(process.env.HYPRIAL_GUI_EXPECTED_ORIGIN || process.env.H2B_GUI_EXPECTED_ORIGIN);
if (!file || !isAbsolute(file)) throw new Error('An absolute launch information path is required');
const child = spawn('dsh', process.argv.slice(2), { stdio: ['inherit', 'pipe', 'pipe'] });
let pending = '', reported = false;
function inspect(chunk) {
  pending += chunk.toString();
  const lines = pending.split('\n');
  pending = lines.pop().slice(-16384);
  for (const line of lines) {
    const match = line.match(/^dsh web: (http:\/\/\S+)/);
    if (!match || reported) continue;
    const url = new URL(match[1]);
    if (url.origin !== expected.origin || url.username || url.password) continue;
    const temporary = file + '.next-' + process.pid;
    try {
      writeFileSync(temporary, JSON.stringify({ schema: process.env.HYPRIAL_GUI_LAUNCH_INFO_FILE ? 'hyprial.gui-launch/v1' : 'h2b.gui-launch/v1', url: url.href }) + '\n', { mode: 0o600, flag: 'wx' });
      renameSync(temporary, file);
      reported = true;
    } finally { try { unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; } }
  }
}
child.stdout.on('data', chunk => { inspect(chunk); process.stdout.write(chunk); });
child.stderr.on('data', chunk => process.stderr.write(chunk));
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(signal, () => child.kill(signal));
child.on('error', () => { console.error('Could not launch DSH'); process.exitCode = 1; });
child.on('close', (code, signal) => { process.exitCode = code ?? (signal ? 128 + ({SIGTERM:15,SIGINT:2,SIGHUP:1}[signal] || 1) : 1); });
