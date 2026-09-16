import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Script, createContext } from 'node:vm';

const MAX_BUNDLE_BYTES = 8 * 1024 * 1024;
const MAX_REGION_BYTES = 128 * 1024;
const conversationRegions = [
  'lib/types/client/contract/conversation.js',
  'lib/types/client/conversation/location-index.js',
  'lib/types/client/conversation/assembler.js',
];
const chatRegions = [
  '../../core/session/src/surface.ts',
  'lib/types/client/conversation-nodes/common.js',
  'lib/types/client/conversation-nodes/event-projection.js',
  'lib/types/client/conversation-nodes/inbox.js',
  'lib/types/client/conversation-nodes/message.js',
];

// Deliberately fail closed on changed bundle layout; never fall back to importing
// an entire UI bundle or to a local reimplementation of the matcher/assembler.
function extractRegion(source, name, filename) {
  const marker = `//#region ${name}`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${filename}: missing region ${name}`);
  assert.equal(source.indexOf(marker, start + marker.length), -1, `${filename}: ambiguous region ${name}`);
  const bodyStart = start + marker.length;
  assert.match(source.slice(bodyStart, bodyStart + 2), /^\r?\n/, `${filename}: invalid region boundary ${name}`);
  const end = source.indexOf('//#endregion', bodyStart);
  assert.ok(end >= 0, `${filename}: unterminated region ${name}`);
  const body = source.slice(bodyStart, end);
  assert.ok(!body.includes('//#region'), `${filename}: nested region ${name}`);
  assert.ok(Buffer.byteLength(body) <= MAX_REGION_BYTES, `${filename}: oversized region ${name}`);
  return body;
}

// Runs wholly inside the VM, including calls into extracted code, so the timeout
// covers assembler operations too. All inputs are synthetic and deeply frozen.
function exerciseOccurrences() {
  const freeze = value => {
    if (value && typeof value === 'object' && !Object.isFrozen(value)) {
      Object.values(value).forEach(freeze);
      Object.freeze(value);
    }
    return value;
  };
  const message = (seq, id, source = { kind: 'user' }) => ({
    type: 'event',
    event: { type: 'user/message', seq, time: 1000 + seq, surfaceOp: 'append',
      data: { id, source, content: [{ type: 'text', text: `synthetic occurrence ${seq}` }] } },
  });
  const splice = (seq, inserted, removedCount = 0) => ({
    type: 'event',
    event: { type: 'agent/inbox/spliced', seq, time: 1000 + seq,
      data: { target: 'next-step', start: 0, removedCount, inserted: inserted.map(id => ({ id })) } },
  });
  const repeatedId = 'synthetic-repeat:["id",1]';
  const entries = freeze([
    message(0, repeatedId), message(1, repeatedId), message(2, 'ordinary-after-repeats'),
    splice(3, [repeatedId]), splice(4, [], 1),
    message(5, repeatedId), message(6, repeatedId), message(7, 'ordinary-after-steering'),
    message(8, 'synthetic-context', { kind: 'plugin', plugin: 'synthetic-verifier', form: 'notice' }),
    message(9, 'synthetic-context', { kind: 'plugin', plugin: 'synthetic-verifier', form: 'notice' }),
  ]);
  const before = JSON.stringify(entries);
  const expectedSeqs = [0, 1, 2, 5, 6, 7, 8, 9];
  const expectedKinds = ['user', 'user', 'user', 'steering', 'steering', 'user', 'context', 'context'];
  const makeAssembler = () => {
    // Only the view sink and registry interfaces are test adapters. Every match,
    // context identity, dependency replay, and view node comes from the runtime.
    const events = { entries: () => [nextStepInboxDefinition, messageDefinition], fallbackEntry: () => undefined };
    const views = { entries: () => [{ target: 'chat', create: () => {
      let nodes = new Map();
      const snapshot = () => [...nodes.values()].sort((a, b) => a.anchorSeq - b.anchorSeq);
      return {
        replace: input => { nodes = new Map(input.nodes.map(node => [node.key, node])); return snapshot(); },
        apply: input => { for (const node of input.upserts) nodes.set(node.key, node); return snapshot(); },
      };
    } }] };
    const assembler = new ConversationNodeAssembler(events, views);
    assembler.activateTarget('chat');
    return assembler;
  };
  const check = (assembler, seqs = expectedSeqs) => {
    assembler.flush();
    const nodes = assembler.snapshot('chat');
    assert.deepEqual(nodes.map(node => node.anchorSeq), seqs, 'all durable occurrences must remain visible');
    assert.equal(new Set(nodes.map(node => node.key)).size, seqs.length, 'occurrence keys must be unique');
    assert.equal(new Set(nodes.map(node => node.id)).size, seqs.length, 'definition-local occurrence IDs must be unique');
    for (const node of nodes) {
      const entry = entries[node.anchorSeq];
      assert.equal(node.kind, expectedKinds[expectedSeqs.indexOf(node.anchorSeq)]);
      assert.equal(node.data.seq, entry.event.seq);
      assert.equal(node.data.time, entry.event.time);
      assert.deepEqual(node.data.content, entry.event.data.content);
      assert.deepEqual(node.data.source, entry.event.data.source);
      if (node.kind === 'steering') assert.equal(node.data.messageId, repeatedId, 'steering must preserve the business data.id');
    }
    return nodes;
  };
  const keys = nodes => nodes.map(node => node.key);

  const replaced = makeAssembler();
  replaced.replaceWindow(entries, false);
  const baseline = keys(check(replaced));
  replaced.replaceWindow(entries, false);
  assert.deepEqual(keys(check(replaced)), baseline, 'resync keys must remain stable');

  const appended = makeAssembler();
  appended.replaceWindow([], false);
  for (const entry of entries) {
    appended.append(entry);
    check(appended, expectedSeqs.filter(seq => seq <= entry.event.seq));
    assert.equal(appended.append(JSON.parse(JSON.stringify(entry))), 'none', 'same-seq live redelivery must be idempotent');
    check(appended, expectedSeqs.filter(seq => seq <= entry.event.seq));
  }
  assert.deepEqual(keys(check(appended)), baseline, 'append and replaceWindow must agree');

  const prepended = makeAssembler();
  // The first page already contains both steering occurrences but not their
  // Inbox dependency; prepending must replay them into steering, not merge IDs.
  prepended.replaceWindow(entries.slice(5), true);
  prepended.flush();
  assert.deepEqual(prepended.snapshot('chat').map(node => node.kind), ['user', 'user', 'user', 'context', 'context']);
  prepended.prepend(entries.slice(3, 5), true);
  check(prepended, [5, 6, 7, 8, 9]);
  prepended.prepend(entries.slice(1, 3), true);
  check(prepended, [1, 2, 5, 6, 7, 8, 9]);
  prepended.prepend(entries.slice(0, 2), false); // overlaps the loaded seq 1
  assert.deepEqual(keys(check(prepended)), baseline, 'prepend and replaceWindow must agree');
  prepended.prepend(entries.map(entry => JSON.parse(JSON.stringify(entry))), false);
  assert.deepEqual(keys(check(prepended)), baseline, 'same-seq older-page redelivery must be idempotent');
  prepended.append(message(10, 'ordinary-final'));
  prepended.flush();
  const final = prepended.snapshot('chat');
  assert.deepEqual(keys(final.slice(0, -1)), baseline);
  assert.equal(final.at(-1).kind, 'user');
  assert.equal(final.at(-1).data.seq, 10, 'ordinary live user after paged repeats must remain visible');

  const replacement = message(11, 'replacement-copy');
  replacement.event.surfaceOp = { startSeq: 0, endSeq: 1 };
  assert.equal(messageDefinition.match(replacement.event), null, 'replacement copies are not append occurrences');
  const notSurface = message(12, 'not-surface');
  delete notSurface.event.surfaceOp;
  assert.equal(messageDefinition.match(notSurface.event), null);
  assert.equal(messageDefinition.match({ type: 'turn/start', seq: 13, data: { turn: 1 } }), null);
  assert.equal(JSON.stringify(entries), before, 'durable business payloads must not be mutated');
  return {
    actualRuntimeMatcher: true, actualRuntimeAssembler: true,
    paths: ['replaceWindow', 'prepend', 'append'],
    durableMessageOccurrences: expectedSeqs.length, steeringOccurrences: 2,
    duplicateSeqIdempotent: true, ordinaryUserRetained: true,
    steeringBusinessIdUnchanged: true, sourceUnchanged: true,
  };
}

/** Verify synthetic durable occurrence semantics against a candidate installation.
 * Reads only the two package bundles below. Region layout drift is an error,
 * never a skip. The VM bounds execution; it is not a security boundary for an
 * untrusted installation. No browser, server, or real conversation is opened.
 */
export async function verifyDshHistoryOccurrences(runtime) {
  assert.ok(typeof runtime === 'string' && runtime.length > 0, 'candidate runtime path is required');
  const snippets = await Promise.all([
    ['dsh-client-ui-conversation', conversationRegions], ['dsh-client-ui-chat', chatRegions],
  ].map(async ([pkg, regions]) => {
    const filename = join(resolve(runtime), 'node_modules', '@deepseek-ai', pkg, 'lib/client.js');
    const source = await readFile(filename, 'utf8');
    assert.ok(Buffer.byteLength(source) <= MAX_BUNDLE_BYTES, `${filename}: oversized UI bundle`);
    return regions.map(name => extractRegion(source, name, filename)).join('\n');
  }));
  const context = createContext({ assert }, { codeGeneration: { strings: false, wasm: false } });
  const program = new Script(`"use strict";\n${snippets.join('\n')}\n(${exerciseOccurrences.toString()})()`, {
    filename: 'dsh-history-occurrences-extracted.vm.js',
  });
  // Convert VM-realm objects to ordinary JSON data for callers and test runners.
  return JSON.parse(JSON.stringify(program.runInContext(context, { timeout: 5000 })));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  console.log(JSON.stringify(await verifyDshHistoryOccurrences(process.argv[2])));
}
