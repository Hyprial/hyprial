import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';

// Exercise the actual audited native sendSession method, with only the host
// prompt boundary replaced. No model request or business session is created.
export async function verifyDshGuiFocusCompatibility(runtime) {
  const root = resolve(runtime);
  const source = readFileSync(join(root, 'node_modules/@deepseek-ai/dsh-client-ui-conversation/lib/client.js'), 'utf8');
  const manifest = JSON.parse(readFileSync(new URL('../packages/dsh-gui-focus-compat/patch.json', import.meta.url), 'utf8'));
  assert.equal(createHash('sha256').update(source).digest('hex'), manifest.files[0].afterSha256);
  const start = source.indexOf('async sendSession(session, text, attachmentIds, mode, signal) {');
  const end = source.indexOf('\n\t\t\t/**', start);
  assert.ok(start > 0 && end > start);
  const sendSession = new Function('nextPaint', 'return ({' + source.slice(start, end) + '}).sendSession;')(() => Promise.resolve());
  const { Context } = await import(pathToFileURL(join(root, 'node_modules/@deepseek-ai/cordis/lib/index.js')).href);
  function fixture(sessionId = 'design') {
    const calls = [], echoes = [], ctx = new Context();
    const image = { id: 'image', kind: 'image', file: { name: 'reference.png' }, previewUrl: 'blob:reference' };
    const file = { id: 'file', kind: 'file', file: { name: 'notes.md' } };
    const service = {
      ctx, resolveDraftAttachments: ids => ids.map(id => ({ image, file })[id]).filter(Boolean),
      fileUploads: { getSnapshot: () => ({ file: { status: 'ready', receiptId: 'receipt', file: { name: 'notes.md' } } }) },
      encodeImage: async () => ({ mimeType: 'image/png', data: 'reference-bytes' }),
      settleSubmittedAttachments() {},
    };
    const session = {
      sessionId, getSnapshot: () => ({ subagent: null }),
      beginSubmission(input) {
        echoes.push(input); queueMicrotask(() => input.onRetire({ reason: 'observed' }));
        return { requestId: 'native-request', abandon() {} };
      },
      async prompt(...args) { calls.push(args); return { ok: true, value: { accepted: true } }; },
    };
    return { calls, echoes, ctx, session, send: (text, ids = [], mode = 'queue', signal) => sendSession.call(service, session, text, ids, mode, signal) };
  }

  const ordinary = fixture();
  await ordinary.send('original', ['image', 'file'], 'steer');
  assert.deepEqual(ordinary.calls[0][0], [
    { type: 'image', mimeType: 'image/png', data: 'reference-bytes' },
    { type: 'file', receiptId: 'receipt' }, { type: 'text', text: 'original' },
  ]);
  assert.equal(ordinary.calls[0][1], 'steer');
  assert.equal(ordinary.calls[0][3], 'native-request');

  const design = fixture();
  let selected = 'page/first', release;
  const saved = new Promise(resolve => { release = resolve; });
  const dispose = design.ctx.on('gui-design/before-send', async request => {
    const snapshot = selected;
    assert.equal(request.sessionId, 'design');
    assert.equal(request.text, 'change this');
    await saved;
    request.contextText = 'draft=draft-1 revision=7 focus=' + snapshot;
  });
  const pending = design.send('change this', ['image', 'file']);
  selected = 'page/second';
  assert.equal(design.calls.length, 0, 'save must finish before admission');
  release(); await pending;
  assert.match(design.calls[0][0][2].text, /revision=7 focus=page\/first\n\nchange this$/);
  assert.equal(design.calls[0][0].length, 3, 'attachments remain part of the same prompt');
  dispose(); await design.send('after editor exit');
  assert.equal(design.calls[1][0][0].text, 'after editor exit');

  const unrelated = fixture('ordinary');
  unrelated.ctx.on('gui-design/before-send', request => {
    if (request.sessionId === 'design') request.contextText = 'design focus';
  });
  await unrelated.send('ordinary chat');
  assert.equal(unrelated.calls[0][0][0].text, 'ordinary chat');

  const rejected = fixture();
  rejected.ctx.on('gui-design/before-send', async () => { throw new Error('draft save conflict'); });
  await assert.rejects(rejected.send('keep my input', ['image']), /draft save conflict/);
  assert.equal(rejected.calls.length, 0);
  assert.equal(rejected.echoes.length, 0, 'failed preparation must not consume draft attachments or create an echo');

  const cancelled = fixture(), controller = new AbortController();
  controller.abort(new Error('cancelled'));
  await assert.rejects(cancelled.send('cancel', [], 'queue', controller.signal), /cancelled/);
  assert.equal(cancelled.calls.length, 0);
  return { status: 'passed', checks: ['native-method-source-hash', 'cordis-async-hook', 'submit-focus-snapshot', 'attachments-and-delivery', 'save-failure-before-admission', 'exit-disposal', 'unrelated-session', 'cancellation'] };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 3) throw new Error('Usage: verify-dsh-gui-focus.mjs candidate-runtime-directory');
  console.log(JSON.stringify(await verifyDshGuiFocusCompatibility(process.argv[2])));
}
