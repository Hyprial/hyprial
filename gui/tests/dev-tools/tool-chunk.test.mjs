// @vitest-environment node
/*
 * Decision record (dev tools seam, PR 4a, 2026-11-14): builds the floating
 * tool layer as a standalone bundle (in memory, nothing written) and checks
 * it with the release artifact gate's own marker list. The follow-up that
 * ships the layer in the release needs a tool chunk without mock data,
 * fixture URLs or scenario data; this test proves the seam already yields
 * one. The only release markers the bundle may hit are the ones that name
 * the tool layer itself (module index ids and copy, the layer's CSS class
 * families); that list is exactly what the follow-up has to reclassify in
 * the artifact gate. The release `dist` and its gate are untouched here.
 * PR 4b-G (2026-11-15): the set of tool layer names is the artifact gate's own
 * export (TOOL_LAYER_OWN) instead of a copy. The standalone library build
 * keeps the component name DevModuleIndex, which the release chunk does not.
 */
import { resolve } from 'node:path';
import { build } from 'vite';
import { describe, expect, it } from 'vitest';
import { TOOL_LAYER_OWN, findProductionCopyViolations } from '../../scripts/verify-production-copy.mjs';

const STANDALONE_ONLY_NAMES = new Set(['DevModuleIndex']);

const SIMULATED_DATA_PROBES = [
  'hyprialScenario',
  'hyprialFixture',
  'hyprialFirstRun',
  'data-mock-scenario',
  'data-dev-scenario-control',
  'agent-squire',
  'studio-mac',
  'edge-demo-1',
  '切换为受邀成员',
  '本地测试数据',
  'J1 场景',
  '真实后端',
  '不提供模拟切换',
  '当前不可用的开发工具',
  'dev-tools-unavailable',
];

async function buildToolLayer() {
  const guiRoot = process.cwd();
  const result = await build({
    root: guiRoot,
    configFile: false,
    logLevel: 'silent',
    build: {
      write: false,
      minify: true,
      lib: {
        entry: resolve(guiRoot, 'src/dev/tools/index.ts'),
        formats: ['es'],
        fileName: 'tools',
      },
      rollupOptions: {
        external: ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime'],
      },
    },
  });
  const outputs = (Array.isArray(result) ? result : [result]).flatMap((entry) => entry.output);
  return outputs.map((item) => (item.type === 'chunk' ? item.code : String(item.source))).join('\n');
}

describe('standalone floating tool layer bundle', () => {
  it('carries no mock data, fixture URL or scenario data', async () => {
    const bundle = await buildToolLayer();

    expect(bundle).toContain('dev-tools-layer');
    expect(SIMULATED_DATA_PROBES.filter((probe) => bundle.includes(probe))).toEqual([]);
    const foreignMarkers = findProductionCopyViolations(bundle)
      .filter((label) => !TOOL_LAYER_OWN.has(label) && !STANDALONE_ONLY_NAMES.has(label));
    expect(foreignMarkers).toEqual([]);
  }, 60_000);
});
