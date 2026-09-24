/**
 * Pi 0.85.1 controlled ResourceLoader boundary.
 *
 * This module composes Pi's public SDK ResourceLoader instead of patching the
 * installed package.  DefaultResourceLoader runs with context discovery and
 * default skill discovery disabled; this boundary supplies only the explicit
 * native root and the approved repository root through cwd.  Consequently an
 * AGENTS.md above the repository and ~/.agents/skills are never opened by the
 * native loader.
 */

import {
  constants,
  closeSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const RECEIPT_SCHEMA = "hyprial-pi-loader-v1";
const CONTEXT_NAMES = ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"];

function fail(message) {
  throw new Error(`PI_CONTROLLED_LOADER: ${message}`);
}

function requireString(value, label) {
  if (typeof value !== "string" || value.length === 0) fail(`${label} must be a non-empty string`);
  return value;
}

function optionalString(value, label) {
  if (value === undefined || value === null) return undefined;
  return requireString(value, label);
}

function stringArray(value, label) {
  if (value === undefined) return [];
  if (!Array.isArray(value)) fail(`${label} must be an array`);
  return value.map((item, index) => requireString(item, `${label}[${index}]`));
}

function absolutePathArray(value, label) {
  return stringArray(value, label).map((path, index) => {
    if (!isAbsolute(path)) fail(`${label}[${index}] must be absolute`);
    const canonical = realpathSync(path);
    const stat = lstatSync(canonical);
    if (!stat.isFile() && !stat.isDirectory()) fail(`${label}[${index}] has unsupported type`);
    return canonical;
  });
}

function absoluteDirectoryArray(value, label) {
  return absolutePathArray(value, label).map((path, index) => {
    if (!lstatSync(path).isDirectory()) fail(`${label}[${index}] must be a directory`);
    assertTreeHasNoLinks(path);
    return path;
  });
}

function canonicalDirectory(value, label) {
  const raw = requireString(value, label);
  if (!isAbsolute(raw)) fail(`${label} must be absolute`);
  let canonical;
  try {
    canonical = realpathSync(raw);
  } catch (error) {
    fail(`${label} is not readable: ${error instanceof Error ? error.message : String(error)}`);
  }
  const stat = lstatSync(canonical);
  if (!stat.isDirectory()) fail(`${label} must be a directory`);
  return canonical;
}

function isWithin(path, root) {
  const offset = relative(root, path);
  return offset === "" || (offset !== ".." && !offset.startsWith(`..${sep}`) && !isAbsolute(offset));
}

function strictRead(path, allowedRoot) {
  const entry = lstatSync(path);
  if (entry.isSymbolicLink() || !entry.isFile()) fail(`resource is not a regular non-symlink file: ${path}`);
  const canonical = realpathSync(path);
  if (!isWithin(canonical, allowedRoot)) fail(`resource escapes its approved root: ${path}`);
  let descriptor;
  try {
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const before = fstatSync(descriptor);
    if (!before.isFile()) fail(`opened resource is not regular: ${path}`);
    const content = readFileSync(descriptor);
    const after = fstatSync(descriptor);
    if (
      before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size ||
      before.mtimeMs !== after.mtimeMs || content.byteLength !== after.size
    ) {
      fail(`resource changed while being read: ${path}`);
    }
    return content;
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
}

function directoriesFromRoot(repositoryRoot, cwd) {
  if (!isWithin(cwd, repositoryRoot)) fail("cwd is outside repositoryRoot");
  const rows = [];
  let current = cwd;
  while (true) {
    rows.unshift(current);
    if (current === repositoryRoot) break;
    const parent = dirname(current);
    if (parent === current) fail("repositoryRoot is not an ancestor of cwd");
    current = parent;
  }
  return rows;
}

function contextFileFromDirectory(directory, allowedRoot, scope) {
  for (const name of CONTEXT_NAMES) {
    const path = join(directory, name);
    let stat;
    try {
      stat = lstatSync(path);
    } catch (error) {
      if (error && error.code === "ENOENT") continue;
      fail(`cannot inspect context candidate ${path}: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (stat.isSymbolicLink() || !stat.isFile()) fail(`context candidate is not a regular non-symlink file: ${path}`);
    const content = strictRead(path, allowedRoot);
    return {
      path: realpathSync(path),
      content: content.toString("utf8").replace(/^\uFEFF/, ""),
      receipt: {
        scope,
        path: realpathSync(path),
        relativePath: relative(allowedRoot, realpathSync(path)) || name,
        digest: createHash("sha256").update(content).digest("hex"),
        size: content.byteLength,
      },
    };
  }
  return undefined;
}

function validateOptions(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) fail("options must be an object");
  if (input.projectTrusted !== true) fail("projectTrusted must be explicitly true");
  const piPackageRoot = canonicalDirectory(input.piPackageRoot, "piPackageRoot");
  const cwd = canonicalDirectory(input.cwd, "cwd");
  const repositoryRoot = canonicalDirectory(input.repositoryRoot, "repositoryRoot");
  const projectionRoot = canonicalDirectory(input.projectionRoot, "projectionRoot");
  const nativeRoot = canonicalDirectory(input.nativeRoot, "nativeRoot");
  const homeRoot = canonicalDirectory(input.homeRoot, "homeRoot");
  if (!isWithin(cwd, repositoryRoot)) fail("cwd is outside repositoryRoot");
  let gitMarker;
  try {
    gitMarker = lstatSync(join(repositoryRoot, ".git"));
  } catch (error) {
    fail(`repositoryRoot has no .git boundary: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!gitMarker.isDirectory() && !gitMarker.isFile()) fail("repositoryRoot .git boundary has unsupported type");
  return { piPackageRoot, cwd, repositoryRoot, projectionRoot, nativeRoot, homeRoot, projectTrusted: true };
}

function explicitSkillPaths(options) {
  const candidates = [{ path: join(options.projectionRoot, "skills"), root: options.projectionRoot }];
  for (const directory of directoriesFromRoot(options.repositoryRoot, options.cwd)) {
    candidates.push(
      { path: join(directory, ".pi", "skills"), root: options.repositoryRoot },
      { path: join(directory, ".agents", "skills"), root: options.repositoryRoot },
    );
  }
  const paths = [];
  for (const candidate of candidates) {
    let stat;
    try {
      stat = lstatSync(candidate.path);
    } catch (error) {
      if (error && error.code === "ENOENT") continue;
      fail(`cannot inspect skill directory ${candidate.path}: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) fail(`skill path is not a regular directory: ${candidate.path}`);
    const canonical = realpathSync(candidate.path);
    if (!isWithin(canonical, candidate.root)) fail(`skill directory escapes its approved root: ${candidate.path}`);
    assertTreeHasNoLinks(canonical);
    paths.push(canonical);
  }
  return paths;
}

function assertTreeHasNoLinks(root) {
  const pending = [root];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isSymbolicLink()) fail(`skill tree contains a symlink: ${path}`);
      if (entry.isDirectory()) pending.push(path);
      else if (!entry.isFile()) fail(`skill tree contains a non-regular entry: ${path}`);
    }
  }
}

function assertStaticResourceSettings(settingsManager) {
  const unsupported = [];
  if (settingsManager.getPackages().length > 0) unsupported.push("packages");
  if (settingsManager.getExtensionPaths().length > 0) unsupported.push("extensions");
  if (settingsManager.getSkillPaths().length > 0) unsupported.push("skills");
  if (settingsManager.getPromptTemplatePaths().length > 0) unsupported.push("prompts");
  if (settingsManager.getThemePaths().length > 0) unsupported.push("themes");
  if (unsupported.length > 0) {
    fail(`dynamic resource settings require a frozen dependency closure: ${unsupported.join(",")}`);
  }
}

function controlledContexts(options) {
  const contexts = [];
  const receipts = [];
  const native = contextFileFromDirectory(options.projectionRoot, options.projectionRoot, "native");
  if (native) {
    contexts.push({ path: native.path, content: native.content });
    receipts.push(native.receipt);
  }
  for (const directory of directoriesFromRoot(options.repositoryRoot, options.cwd)) {
    const project = contextFileFromDirectory(directory, options.repositoryRoot, "project");
    if (!project) continue;
    contexts.push({ path: project.path, content: project.content });
    receipts.push(project.receipt);
  }
  return { contexts, receipts };
}

function skillScope(path, options, approvedSkillRoots) {
  if (isWithin(path, options.projectionRoot)) return "native";
  if (isWithin(path, options.repositoryRoot)) return "project";
  if (approvedSkillRoots.some((root) => isWithin(path, root))) return "approved";
  fail(`Pi SDK returned a skill outside approved roots: ${path}`);
}

export async function createControlledPiResourceLoader(rawOptions) {
  const options = validateOptions(rawOptions);
  // The native loader's independent ~/.agents/skills source must resolve
  // against P22's explicit HOME, never the process that launched Hyprial.
  process.env.HOME = options.homeRoot;
  const manifest = JSON.parse(readFileSync(join(options.piPackageRoot, "package.json"), "utf8"));
  if (manifest.name !== "@earendil-works/pi-coding-agent") fail("piPackageRoot names a different package");
  const sdk = await import(pathToFileURL(join(options.piPackageRoot, "dist", "index.js")).href);
  const settingsManager = sdk.SettingsManager.create(options.cwd, options.projectionRoot, { projectTrusted: true });
  assertStaticResourceSettings(settingsManager);
  const approvedSkillRoots = absoluteDirectoryArray(rawOptions.additionalSkillPaths, "additionalSkillPaths");
  const delegate = new sdk.DefaultResourceLoader({
    cwd: options.cwd,
    agentDir: options.projectionRoot,
    settingsManager,
    noContextFiles: true,
    noSkills: true,
    additionalSkillPaths: [...explicitSkillPaths(options), ...approvedSkillRoots],
    additionalExtensionPaths: absolutePathArray(rawOptions.additionalExtensionPaths, "additionalExtensionPaths"),
    appendSystemPrompt: stringArray(rawOptions.appendSystemPrompt, "appendSystemPrompt"),
  });
  let contexts = { contexts: [], receipts: [] };
  const loader = {
    getExtensions: () => delegate.getExtensions(),
    getSkills: () => delegate.getSkills(),
    getPrompts: () => delegate.getPrompts(),
    getThemes: () => delegate.getThemes(),
    getAgentsFiles: () => ({ agentsFiles: contexts.contexts }),
    getSystemPrompt: () => delegate.getSystemPrompt(),
    getSystemPromptSource: () => delegate.getSystemPromptSource(),
    getAppendSystemPrompt: () => delegate.getAppendSystemPrompt(),
    getAppendSystemPromptSources: () => delegate.getAppendSystemPromptSources(),
    extendResources: (paths) => delegate.extendResources(paths),
    reload: async (reloadOptions) => {
      await delegate.reload(reloadOptions);
      contexts = controlledContexts(options);
    },
  };
  await loader.reload();
  const receiptForSystemPrompt = (systemPrompt) => ({
    schema: RECEIPT_SCHEMA,
    piPackageVersion: requireString(manifest.version, "pi package version"),
    cwd: options.cwd,
    repositoryRoot: options.repositoryRoot,
    projectionRoot: options.projectionRoot,
    nativeRoot: options.nativeRoot,
    homeRoot: options.homeRoot,
    projectTrusted: true,
    contextFiles: contexts.receipts,
    skills: loader.getSkills().skills.map((skill) => ({
      name: skill.name,
      path: realpathSync(skill.filePath),
      scope: skillScope(realpathSync(skill.filePath), options, approvedSkillRoots),
    })),
    effectiveSettings: {
      defaultProvider: settingsManager.getDefaultProvider() ?? null,
      defaultModel: settingsManager.getDefaultModel() ?? null,
      defaultThinkingLevel: settingsManager.getDefaultThinkingLevel() ?? null,
    },
    sessionProof: {
      systemPromptDigest: createHash("sha256").update(systemPrompt).digest("hex"),
      presentContextDigests: contexts.contexts
        .map((context, index) => ({ context, digest: contexts.receipts[index].digest }))
        .filter(({ context }) => systemPrompt.includes(context.content))
        .map(({ digest }) => digest),
    },
  });
  const receipt = async () => {
    const created = await sdk.createAgentSession({
      cwd: options.cwd,
      agentDir: options.nativeRoot,
      resourceLoader: loader,
      settingsManager,
      sessionManager: sdk.SessionManager.inMemory(options.cwd),
    });
    let systemPrompt;
    try {
      systemPrompt = created.session.systemPrompt;
    } finally {
      created.session.dispose();
    }
    return receiptForSystemPrompt(systemPrompt);
  };
  return { loader, receipt, receiptForSystemPrompt, settingsManager, sdk, options };
}

async function openOrCreateSession(sdk, cwd, sessionRoot, sessionId) {
  const existing = (await sdk.SessionManager.list(cwd, sessionRoot)).find((session) => session.id === sessionId);
  if (existing) return sdk.SessionManager.open(existing.path, sessionRoot);
  return sdk.SessionManager.create(cwd, sessionRoot, { id: sessionId });
}

function atomicWriteReceipt(path, sessionRoot, receipt) {
  const configured = requireString(path, "receiptPath");
  if (!isAbsolute(configured)) fail("receiptPath must be absolute");
  const absolute = resolve(configured);
  const parent = dirname(absolute);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const canonicalParent = realpathSync(parent);
  if (!isWithin(canonicalParent, sessionRoot)) fail("receiptPath must stay inside sessionRoot");
  const target = join(canonicalParent, basename(absolute));
  const temporary = `${target}.tmp-${process.pid}-${randomUUID()}`;
  writeFileSync(temporary, `${JSON.stringify(receipt)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  renameSync(temporary, target);
}

async function createControlledRuntime(rawOptions) {
  const sessionRoot = canonicalDirectory(rawOptions.sessionRoot, "sessionRoot");
  const sessionId = requireString(rawOptions.sessionId, "sessionId");
  const receiptPath = requireString(rawOptions.receiptPath, "receiptPath");
  const modelProvider = optionalString(rawOptions.modelProvider, "modelProvider");
  const modelId = optionalString(rawOptions.model, "model");
  if ((modelProvider === undefined) !== (modelId === undefined)) {
    fail("modelProvider and model must be supplied together");
  }
  let initial = true;
  const createRuntime = async ({ cwd, sessionManager, sessionStartEvent }) => {
    const controlled = await createControlledPiResourceLoader({ ...rawOptions, cwd });
    const sdk = controlled.sdk;
    const extensionErrors = controlled.loader.getExtensions().errors;
    if (extensionErrors.length > 0) {
      fail(`Pi extension load failed: ${extensionErrors.map((entry) => `${entry.path}: ${entry.error}`).join("; ")}`);
    }
    const modelRuntime = await sdk.ModelRuntime.create({
      authPath: join(controlled.options.nativeRoot, "auth.json"),
      modelsPath: join(controlled.options.nativeRoot, "models.json"),
      modelsStorePath: join(controlled.options.nativeRoot, "models-store.json"),
    });
    const model = modelProvider === undefined ? undefined : modelRuntime.getModel(modelProvider, modelId);
    if (modelProvider !== undefined && model === undefined) {
      fail(`configured Pi model is unavailable: ${modelProvider}/${modelId}`);
    }
    const services = {
      cwd: controlled.options.cwd,
      agentDir: controlled.options.nativeRoot,
      modelRuntime,
      settingsManager: controlled.settingsManager,
      resourceLoader: controlled.loader,
      diagnostics: [],
    };
    const created = await sdk.createAgentSessionFromServices({
      services,
      sessionManager,
      sessionStartEvent,
      model,
    });
    atomicWriteReceipt(
      receiptPath,
      sessionRoot,
      controlled.receiptForSystemPrompt(created.session.systemPrompt),
    );
    if (initial) {
      const sessionName = optionalString(rawOptions.sessionName, "sessionName");
      if (sessionName !== undefined) sessionManager.appendSessionInfo(sessionName);
      initial = false;
    }
    return { ...created, services, diagnostics: [] };
  };
  const options = validateOptions(rawOptions);
  const sdk = await import(pathToFileURL(join(options.piPackageRoot, "dist", "index.js")).href);
  const sessionManager = await openOrCreateSession(
    sdk,
    options.cwd,
    sessionRoot,
    sessionId,
  );
  return sdk.createAgentSessionRuntime(createRuntime, {
    cwd: options.cwd,
    agentDir: options.nativeRoot,
    sessionManager,
  });
}

async function runControlledMode(command, options) {
  const runtime = await createControlledRuntime(options);
  const sdk = await import(pathToFileURL(join(canonicalDirectory(options.piPackageRoot, "piPackageRoot"), "dist", "index.js")).href);
  if (command === "run-rpc") {
    await sdk.runRpcMode(runtime);
    return;
  }
  const interactive = new sdk.InteractiveMode(runtime, {
    initialMessage: optionalString(options.initialMessage, "initialMessage"),
  });
  try {
    await interactive.run();
  } finally {
    await runtime.dispose();
  }
}

async function main() {
  const [command, payload, ...rest] = process.argv.slice(2);
  if (!new Set(["probe", "run-rpc", "run-tui"]).has(command) || payload === undefined || rest.length !== 0) {
    fail("usage: node pi_controlled_loader.mjs <probe|run-rpc|run-tui> '<json>'");
  }
  let options;
  try {
    options = JSON.parse(payload);
  } catch (error) {
    fail(`probe JSON is invalid: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (command === "probe") {
    const controlled = await createControlledPiResourceLoader(options);
    process.stdout.write(`${JSON.stringify(await controlled.receipt())}\n`);
    return;
  }
  await runControlledMode(command, options);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
