import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MODULE_EXPORTS = {
  core: ['CORE_TRANSPORT_KINDS', 'createChatCore', 'createDemoConnection', 'mountChat', 'startBrowserChat'],
  studio: ['createStudio', 'registerStudioTools'],
  session: ['createMockSessionAdapter'],
  host: ['createHost'],
  client: ['createComposer', 'createUiRuntime', 'createWorkspaceRenderer'],
  presentation: ['GUI_STYLE_CONTRACT', 'guiValidateTheme', 'guiValidateAppearance', 'guiStyleVariables', 'guiAppearanceStyle', 'guiStyleAliases', 'guiModuleAppearanceVariables'],
  transport: ['BUILTIN_TRANSPORT_DRIVERS', 'createTransportDriver'],
  product: ['createGuiServer', 'resolveProductConfig'],
};
const MODULES = new Set(Object.keys(MODULE_EXPORTS));
const TRUSTED_PLUGIN_INPUTS = ['plugins/trusted-panel.mjs'];
const ALLOWED_DEPENDENCIES = {
  core: [],
  host: ['session', 'studio', 'client'],
  studio: ['presentation'],
  client: ['presentation'],
  session: [],
  presentation: [],
  transport: ['session'],
  product: ['client', 'host', 'studio', 'transport'],
};
const FORBIDDEN_PACKAGES = /^(?:@deepseek-ai\/dsh(?:$|-)|openai(?:$|\/)|@anthropic-ai\/|@openai\/codex)/;

async function sourceFiles(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const result = [];
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
    const path = join(root, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`Runtime source symlink is unsupported: ${path}`);
    if (entry.isDirectory()) result.push(...await sourceFiles(path));
    else if (/\.(?:mjs|js)$/.test(entry.name)) result.push(path);
  }
  return result;
}

function importSpecifiers(source) {
  return [...source.matchAll(/\b(?:import|export)\s+(?:[\s\S]*?\sfrom\s*)?['"]([^'"]+)['"]/g),
    ...source.matchAll(/\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g)].map(match => match[1]);
}

/**
 * The app core runs in a browser/WebView webview with no Node and no bundler
 * shim, so the whole import closure of its public face must stay inside
 * `core/` and may only use relative specifiers. Walking the closure from
 * `index.mjs` keeps Node-capable build tooling out of the shipped runtime.
 */
async function coreClosureProblems(root) {
  const problems = [];
  const queue = ['index.mjs'];
  const seen = new Set();
  while (queue.length) {
    const name = queue.shift();
    if (seen.has(name)) continue;
    seen.add(name);
    const path = join(root, 'core', name);
    let source;
    try {
      source = await readFile(path, 'utf8');
    } catch {
      problems.push(`core/${name}: app core runtime file is missing`);
      continue;
    }
    for (const specifier of importSpecifiers(source)) {
      if (!specifier.startsWith('./')) {
        problems.push(`core/${name}: app core runtime must not import '${specifier}' (Node builtin or external dependency)`);
        continue;
      }
      const target = relative(join(root, 'core'), resolve(dirname(path), specifier)).split(sep).join('/');
      if (target.startsWith('..')) problems.push(`core/${name}: '${specifier}' escapes the app core module`);
      else queue.push(target);
    }
  }
  return problems;
}

// A bounded static guard for this one-off, static-ESM reconstruction. This is
// intentionally not a generic JavaScript parser or a proof of all data flow.
export async function checkArchitecture(root) {
  const problems = [];
  const dependencies = [];
  let checked = 0;
  const pluginFiles = await sourceFiles(join(root, 'plugins'));
  const pluginInputs = pluginFiles.map(file => relative(root, file).split(sep).join('/')).sort();
  if (JSON.stringify(pluginInputs) !== JSON.stringify(TRUSTED_PLUGIN_INPUTS)) {
    problems.push('trusted plugin inventory differs from the fixed product inputs');
  }
  // Scan the complete selected source closure before importing any public face.
  // A rejected plugin must never execute merely because its guard is running.
  for (const module of [...MODULES, 'plugins']) {
    const files = module === 'plugins' ? pluginFiles : await sourceFiles(join(root, module));
    for (const file of files) {
      checked++;
      const source = await readFile(file, 'utf8');
      const path = relative(root, file).split(sep).join('/');
      if (/\b(?:eval\s*\(|new\s+Function\s*\(|cordis_define\b|__ModuleLoader__\b)/.test(source)) {
        problems.push(`${path}: runtime code evaluation/module loader is forbidden`);
      }
      const specifiers = importSpecifiers(source);
      for (const specifier of specifiers) {
        if (FORBIDDEN_PACKAGES.test(specifier) || ['node:child_process', 'child_process'].includes(specifier)) {
          problems.push(`${path}: excluded execution dependency ${specifier}`);
        }
        if (!specifier.startsWith('.')) continue;
        const target = resolve(dirname(file), specifier);
        const targetRelative = relative(root, target).split(sep).join('/');
        const targetModule = targetRelative.split('/')[0];
        if (targetRelative === '..' || targetRelative.startsWith('../')) {
          problems.push(`${path}: source dependency escapes the new GUI ${specifier}`);
        }
        if (targetModule === 'plugins' && targetModule !== module) {
          if (module !== 'product' || !TRUSTED_PLUGIN_INPUTS.includes(targetRelative)) {
            problems.push(`${path}: unreviewed trusted plugin import ${specifier}`);
          }
        } else if (!MODULES.has(targetModule) && targetModule !== 'plugins') {
          problems.push(`${path}: unreviewed runtime source import ${specifier}`);
        }
        if (MODULES.has(targetModule) && targetModule !== module) {
          dependencies.push({ from: module, to: targetModule, path, specifier });
          if (targetRelative !== `${targetModule}/index.mjs`) {
            problems.push(`${path}: cross-module implementation import ${specifier}`);
          }
          if (!(ALLOWED_DEPENDENCIES[module] ?? []).includes(targetModule)) {
            problems.push(`${path}: ${module} must consume injected ports, not import ${targetModule}`);
          }
        }
      }
    }
  }
  if (MODULES.has('core')) problems.push(...await coreClosureProblems(root));
  if (!problems.length) for (const module of MODULES) {
    const facade = join(root, module, 'index.mjs');
    const namespace = await import(pathToFileURL(facade));
    const actual = Object.keys(namespace).sort();
    const expected = [...MODULE_EXPORTS[module]].sort();
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      problems.push(`${module}: public exports ${actual.join(',')} differ from ${expected.join(',')}`);
    }
  }
  if (!checked) problems.push('No runtime source covered');
  return { status: problems.length ? 'FAIL' : 'PASS', checkedFiles: checked, trustedPluginInputs: pluginInputs, dependencies, problems };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  try {
    const result = await checkArchitecture(root);
    console.log(JSON.stringify(result, null, 2));
    process.exitCode = result.status === 'PASS' ? 0 : 1;
  } catch (error) {
    console.error(`Architecture check failed: ${error.message}`);
    process.exitCode = 1;
  }
}
