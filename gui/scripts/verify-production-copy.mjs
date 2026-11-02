import { readFile, readdir } from 'node:fs/promises';
import { extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const FORBIDDEN_COPY = [
  { label: 'reference-mock-proxy', pattern: /\/api\/reference\/mock-/u },
  { label: 'reference-sentinel', pattern: /example-not-a-credential|ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs/u },
  { label: 'reference-mock-service', pattern: /MOCK_SCENARIO_MISSING/u },
  { label: 'dev-01', pattern: /dev-0[1-6]/u },
  { label: 'fixture', pattern: /fixture/iu },
  { label: '演示', pattern: /演示/u },
  { label: '样例', pattern: /样例/u },
  { label: '占位', pattern: /占位/u },
  { label: '开发预览', pattern: /开发预览/u },
  { label: '本地测试数据', pattern: /本地测试数据/u },
  { label: '场景', pattern: /场景/u },
  { label: 'dev-data-source', pattern: /dev-data-source/u },
  { label: 'dev-module-index', pattern: /dev-module-index/u },
  { label: 'dev-module-dock', pattern: /dev-module-dock/u },
  { label: 'dev-module-entry', pattern: /dev-module-entry/u },
  { label: 'dev-preview-frame', pattern: /dev-preview-frame/u },
  { label: 'dev-module-scroll-hint', pattern: /dev-module-scroll-hint/u },
  { label: 'studio-mac', pattern: /studio-mac/u },
  { label: 'travel-mac', pattern: /travel-mac/u },
  { label: '合成系统', pattern: /合成系统/u },
  { label: '本地静态演示', pattern: /本地静态演示/u },
  { label: '不是实时数据', pattern: /不是实时数据/u },
  { label: 'edge-demo-1', pattern: /edge-demo-1/u },
  { label: '2026-10-22 10:15', pattern: /2026-10-22 10:15/u },
  { label: 'GUI-LINK-001', pattern: /GUI-LINK-001/u },
  { label: 'GUI-ORG-001', pattern: /GUI-ORG-001/u },
  { label: 'dev-local', pattern: /dev-local/u },
  { label: '切换为受邀成员', pattern: /切换为受邀成员/u },
  { label: '切换为创建者', pattern: /切换为创建者/u },
  { label: 'Mock', pattern: /Mock/u },
  { label: '接口未接通', pattern: /接口未接通/u },
  { label: '邀请码演示', pattern: /邀请码演示/u },
  { label: '服务接入前', pattern: /服务接入前/u },
  { label: '不会改变账号', pattern: /不会改变账号/u },
  { label: '服务尚未接入', pattern: /服务尚未接入/u },
  { label: '未执行任何操作', pattern: /未执行任何操作/u },
  { label: '不会更改连接状态', pattern: /不会更改连接状态/u },
  { label: 'add-failure', pattern: /add-failure/u },
  { label: 'authorize-failure', pattern: /authorize-failure/u },
];
const TEXT_EXTENSIONS = new Set(['.css', '.html', '.js', '.json', '.map', '.txt']);

export function findProductionCopyViolations(copy) {
  return FORBIDDEN_COPY
    .filter(({ pattern }) => pattern.test(copy))
    .map(({ label }) => label);
}

async function readBundleText(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const chunks = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      chunks.push(await readBundleText(path));
    } else if (TEXT_EXTENSIONS.has(extname(entry.name))) {
      chunks.push(await readFile(path, 'utf8'));
    }
  }
  return chunks.join('\n');
}

async function main() {
  const directory = resolve(process.argv[2] ?? 'dist');
  const violations = findProductionCopyViolations(
    await readBundleText(directory),
  );
  if (violations.length > 0) {
    throw new Error(
      `Production copy contains forbidden markers: ${violations.join(', ')}`,
    );
  }
  process.stdout.write('Production copy gate passed: 0 forbidden markers.\n');
}

const isMain = process.argv[1]
  ? pathToFileURL(resolve(process.argv[1])).href === import.meta.url
  : false;
if (isMain) {
  await main();
}
