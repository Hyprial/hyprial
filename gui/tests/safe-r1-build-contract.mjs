import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const outputRoot = mkdtempSync(join(tmpdir(), 'hyprial-safe-r1-build-'));
const developmentOutput = join(outputRoot, 'development');
const productionOutput = join(outputRoot, 'production');
const moduleIds = Array.from({ length: 6 }, (_, index) => `dev-0${index + 1}`);
const developmentClassNames = [
  'dev-data-source',
  'dev-module-index',
  'dev-module-entry',
  'dev-tools-layer',
  'dev-tools-fab',
  'dev-tools-panel',
  'dev-scenario-control',
];
const forbiddenProductionCopy = /dev-0[1-6]|fixture|演示|样例|占位服务|开发预览|本地测试数据|场景|进行中任务|待你决定|当前看板|还没有安排|暂无进行中的事项/i;

function filesUnder(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(path) : [path];
  });
}

function readableBundle(directory) {
  return filesUnder(directory)
    .filter((path) => statSync(path).isFile())
    .filter((path) => /\.(?:html|css|js|json|map)$/i.test(path))
    .map((path) => `\n/* ${relative(directory, path)} */\n${readFileSync(path, 'utf8')}`)
    .join('');
}

function build(mode, output) {
  execFileSync(
    process.execPath,
    [
      resolve(root, 'node_modules/vite/bin/vite.js'),
      'build',
      '--mode',
      mode,
      '--outDir',
      output,
      '--emptyOutDir',
    ],
    {
      cwd: root,
      env: {
        ...process.env,
        HYPRIAL_DEV_MODULE_LABELS: '1',
        HYPRIAL_GUI_DEV_PREVIEW: '1',
      },
      stdio: 'inherit',
    },
  );
}

try {
  build('development', developmentOutput);
  build('production', productionOutput);
  const failures = [];
  const development = readableBundle(developmentOutput);
  if (!development.includes('开发工具')) {
    failures.push('development build is missing the floating tools toggle');
  }
  if (!development.includes('开发模块索引')) {
    failures.push('development build is missing the 开发模块索引 panel');
  }
  for (const moduleId of moduleIds) {
    if (!development.includes(moduleId)) {
      failures.push(`development build is missing ${moduleId}`);
    }
  }
  if (!development.includes('开发预览')) {
    failures.push('development build is missing the preview label');
  }
  if (!development.includes('本地测试数据')) {
    failures.push('development build is missing the data-source notice');
  }

  const production = readableBundle(productionOutput);
  for (const className of developmentClassNames) {
    if (production.includes(className)) {
      failures.push(
        `production build contains development class name: ${className}`,
      );
    }
  }
  const forbidden = production.match(forbiddenProductionCopy)?.[0];
  if (forbidden) {
    failures.push(`production build contains forbidden preview copy: ${forbidden}`);
  }
  // J6 development seeds must not reach the production bundle.
  for (const synthetic of [
    'studio-mac',
    'travel-mac',
    '合成系统',
    '本地静态演示',
    '不是实时数据',
    'edge-demo-1',
    '2026-10-22 10:15',
    'GUI-LINK-001',
    'GUI-ORG-001',
    'dev-local',
    '切换为受邀成员',
    '切换为创建者',
    '邀请码演示',
  ]) {
    if (production.includes(synthetic)) {
      failures.push(`production build contains synthetic data: ${synthetic}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(failures.join('\n'));
  }

  console.log('safe R1 build contract passed');
} finally {
  rmSync(outputRoot, { recursive: true, force: true });
}
