import assert from 'node:assert/strict';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { verifyDshHistoryOccurrences } from './verify-dsh-history-occurrences.mjs';
export async function verifyDshHistoryCompatibility(runtime) {
  const moduleAt = (pkg, file = 'lib/index.js') => import(pathToFileURL(join(resolve(runtime), 'node_modules/@deepseek-ai', pkg, file)).href);
  const [format, oldFormat, currentFormat, subagent, descriptorModule] = await Promise.all([
    moduleAt('dsh-session-format'), moduleAt('dsh-session-format-v0-to-v1'),
    moduleAt('dsh-session-format-v2-to-v3'), moduleAt('dsh-subagent'),
    moduleAt('dsh-subagent', 'lib/types/descriptor.js'),
  ]);
  const header = { version: 2, id: 'gui-history-compat-fixture', createdAt: 1, isSeeded: false, delegationDepth: 0 };
  for (const data of [
    { version: 2, mode: 'one-shot', provider: 'spawn' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'empty persona', persona: '' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'historical child', agentProvider: 'mock', agentModel: 'mock', persona: 'preserve', toolFilter: { deny: ['write'] } },
    { version: 3, mode: 'continuable', provider: 'spawn', label: 'current child', agentProvider: 'mock', agentModel: 'mock', agentReasoningEffort: 'high' },
  ]) {
    const source = { type: 'subagent/descriptor', seq: 0, time: 1, data };
    const before = JSON.stringify(source);
    oldFormat.assertReleasedEventPayload(source, 0);
    const targetHeader = currentFormat.sessionFormatV2ToV3.migrateHeader(header);
    const stage = currentFormat.sessionFormatV2ToV3.createStage({ sourceHeader: header, targetHeader, sourceInheritedEventCount: 0, sourceKind: 'decoded' });
    const collector = new format.SessionFormatEventCollector();
    stage.transformEvent(source, collector);
    const artifact = { header: targetHeader, inheritedEventCount: stage.finish(collector), events: collector.values };
    const restored = currentFormat.restoreReleasedV3Artifact(artifact, new Set());
    assert.deepEqual(restored.events[0].data, data);
    for (const owner of [subagent, descriptorModule]) assert.deepEqual(owner.foldSubagentDescriptor(restored.events), { ...data, version: 3 });
    assert.equal(JSON.stringify(source), before);
  }
  for (const data of [
    { version: 4, mode: 'one-shot', provider: 'spawn' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'invalid persona', persona: false },
    { version: 3, mode: 'continuable', provider: 'spawn', label: 'current invalid persona', persona: '' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'child', agentReasoningEffort: 'high' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'child', agentProvider: 'unpaired' },
    { version: 2, mode: 'continuable', provider: 'spawn', label: 'child', toolFilter: { deny: [1] } },
  ]) assert.throws(() => oldFormat.assertReleasedEventPayload({ type: 'subagent/descriptor', seq: 0, time: 1, data }, 0));
  return { migratedDescriptors: 4, rejectedMalformedDescriptors: 6, sourceUnchanged: true, bundleAndInternalEntry: true, occurrences: await verifyDshHistoryOccurrences(runtime) };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) console.log(JSON.stringify(await verifyDshHistoryCompatibility(process.argv[2])));
