import { EventEmitter } from 'node:events';
import { describe, expect, it } from 'vitest';
import { logsOf, waitForStartup } from '../support/cli.mjs';

describe('actual GUI startup diagnostics', () => {
  it('preserves the startup error when build stderr exceeds the diagnostic budget', async () => {
    const child = new EventEmitter();
    const failure = JSON.stringify({ ok: false, error: { code: 'GUI_START_FAILED', message: 'synthetic startup failure' } });
    const capture = {
      stdout: `${failure}\n`,
      stderr: 'synthetic build output\n'.repeat(1_000),
      listeners: new Set(),
    };
    const startup = waitForStartup(child, capture);
    child.emit('exit', 1, null);
    await expect(startup).rejects.toThrow(failure);
    expect(capture.listeners.size).toBe(0);
    expect(child.listenerCount('exit')).toBe(0);
  });

  it('keeps both stream heads and tails in a bounded diagnostic', () => {
    const diagnostic = logsOf({
      stdout: `stdout first error${'o'.repeat(64_000)}stdout failure detail`,
      stderr: `stderr first error${'e'.repeat(64_000)}stderr failure detail`,
    });
    expect(diagnostic).toContain('stdout:\n');
    expect(diagnostic).toContain('stdout first error');
    expect(diagnostic).toContain('stdout failure detail');
    expect(diagnostic).toContain('stderr:\n');
    expect(diagnostic).toContain('stderr first error');
    expect(diagnostic).toContain('stderr failure detail');
    expect(diagnostic.length).toBeLessThanOrEqual(12_000);
  });
});
