#!/usr/bin/env node
/**
 * Fail closed when the GUI grows a request path or a data-serving host outside daemon request.
 *
 * Decision record (2026-11-14, GUI launch-path convergence, PR 1e):
 * - Owner rule: every GUI API path goes through daemon request. The browser side
 *   has one transport module (transportFiles); nothing else under gui/src may
 *   reference a network primitive, construct the transport, import an HTTP
 *   client library or name a request path. This gate scans all of gui/src,
 *   including gui/src/dev and the generated client, and also the inline
 *   scripts of the html files; there is no directory exemption and `dist` or
 *   `node_modules` inside gui/src are scanned like any other directory.
 * - Network primitives are found by identifier, by member access on a global
 *   chain (`globalThis`, `window`, `self`, `navigator`, `window.navigator`,
 *   `globalThis.window`) in dotted, bracket, destructured and `Reflect.get`
 *   form. The three legacy prefixes (bannedPrefixes) are findings anywhere
 *   inside a string or template literal, mid-string and after `${...}` too.
 * - The host side has no Node data-serving host: the Python session host owns
 *   /daemon/* and the one sanctioned Node server is the Contract Mock
 *   (gui/contract-mock/serve.ts). Tests, scripts, every vite/vitest/playwright
 *   config and every server directory, gui/server included, may not mount a route handler (`configureServer`,
 *   `middlewares.use` including through an alias), start an HTTP/TCP server
 *   (named, default, namespace, dynamic or required node:http/https/http2/net/
 *   tls, a server library, or vite createServer with plugins) or answer a
 *   browser request with canned data (`fulfill`, also by bracket access).
 *   A server finding is keyed by its enclosing function so distinct servers in
 *   one file need distinct ledger rows.
 * - A URL whose host is under the reserved `.invalid` top-level domain cannot be
 *   requested and is not a finding.
 * - One ledger file holds both lists, so neither can grow in the pull request
 *   that adds the violation. `allowlist` rows are exceptions that are meant to
 *   stay for now, each with its own reason; `pending` rows are work that later
 *   stages of PR 1e still have to remove. A row that no longer matches its
 *   finding fails, so neither list rots.
 * - Both lists can only shrink. With --base-ref the ledger at the base commit
 *   is the reference: a row that is new, or larger than at the base, fails.
 *   The base ref is mandatory when given: an empty or unresolvable ref, or an
 *   unreadable base ledger, fails closed. The first seed is accepted only when
 *   the base has no ledger at all, only with --allow-first-seed on a dev/main
 *   base and only if the ledger hashes to FIRST_SEED_LEDGER_SHA256; once the
 *   base carries a ledger the digest is never consulted.
 * - The scanner configuration (gui-request-paths-scanner.json: patterns, scan
 *   roots, skips, extensions, banned prefixes, ledger path) is data, read from
 *   HEAD and from the --base-ref blob. It is narrowed when a pattern, root,
 *   extension, library or prefix is removed or changed, a skip or exemption is
 *   added, a key is removed or unknown, or the ledger path changes; that fails.
 *   Additions of patterns, roots and extensions and removals of skips pass.
 *   Any other change that needs a human decision is cleared only by
 *   REVIEWED_SCANNER_SHA256: when the digest of HEAD's configuration
 *   (--print-scanner-digest) equals the pinned constant, the change passes, so
 *   changing the scanner always edits this reviewed script in the same PR. A
 *   base that has a ledger but no configuration needs that pin as well; a base
 *   without a ledger is the first seed of both.
 * - Out of reach, by design: the scanning logic of this script and of its test
 *   file is not pinned (a change to it is code review), and a request path
 *   assembled by string construction, computed member access, an inline event
 *   handler attribute, a promise chain on a dynamic import or an indirect call
 *   is not seen. The ledger rows and the configuration are the part a pull
 *   request cannot weaken silently.
 * - Ledger rows have an exact key set and a known finding kind; anything else
 *   fails instead of being ignored.
 * - 2026-11-15 (feature-area integration): while the base still has no ledger,
 *   FIRST_SEED_LEDGER_SHA256 names the ledger this branch carries. The seed
 *   lost six pending rows whose code the integration deleted (the HTTP agent
 *   provider factory, its dev mock URL and gui/tests/live/browser.mjs); it
 *   gained none. Once dev carries a ledger the digest is not consulted.
 * - 2026-11-15 (one JavaScript Contract Mock, PR 1e-2): gui/contract-mock/ is a
 *   second sanctioned host root next to gui/server/daemon-request/ (deleted in
 *   PR 3, see the last note). It is the
 *   one module that serves the mock over loopback HTTP (Node form) and over
 *   a fetch-shaped function (browser form); it serves canned contract examples
 *   by design and no business data of its own. Both roots are listed in
 *   hostRoots and sanctionedHostRoots, and adding an exemption is the change
 *   REVIEWED_SCANNER_SHA256 clears, so the pin moved to the digest of this
 *   configuration. The mock-source gate (check_gui_mock_source.mjs) owns what
 *   may import that module and what its code may name.
 * - 2026-11-15 (1e-2 after review): the exemption is narrowed from the whole
 *   directory to the one file that needs it, gui/contract-mock/serve.ts (the
 *   loopback server); the rest of gui/contract-mock stays scanned. The review
 *   pin is a PR-local clearance, not a standing exception: it is non-null
 *   here only because this PR adds a sanctioned root against a base whose pin
 *   is null, and the next PR that touches the scanner configuration or this
 *   script must reset REVIEWED_SCANNER_SHA256 to null (the self-test accepts a
 *   non-null pin only if it equals the committed scanner digest). Tracked in
 *   docs/reference/known-problems.md KP-063.
 * - 2026-11-15 (browser scripts on the Contract Mock, PR 1e-3): the seed
 *   digest moved to the shrunken ledger. Four pending rows left (the org
 *   request script's route.fulfill and two path literals, and the reference
 *   entry that only the Agent second-submission script used); none was added. REVIEWED_SCANNER_SHA256 is untouched here: its reset is
 *   the owner's call (see the note above).
 * - 2026-11-16 (one launch path, PR 3): gui/server/daemon-request/ is deleted
 *   with the Node host and leaves sanctionedHostRoots, which only shrinks, so
 *   the base comparison passes without a review pin and REVIEWED_SCANNER_SHA256
 *   is reset to null as the notes above require. gui/server stays in hostRoots
 *   on purpose: a Node server directory that comes back is then a finding with
 *   no exemption and cannot enter the ledger.
 * - 2026-11-16 (PR 1e-4): the private /api/reference/* test host, its entries and
 *   the two org browser scripts with route.fulfill are deleted. gui/scripts/serve.mjs
 *   no longer injects Vite plugins, so its allowlist row left with its finding.
 *   One pending row stays: the fake upstream listener of the Python host
 *   conformance suite (gui/tests/daemon-request-host/host.test.ts). Moving it to
 *   allowlist would grow that list against the base; KP-063 records the options.
 * - 2026-11-16 (owner decision, KP-063 closed): gui/tests/daemon-request-host/host.test.ts
 *   joins sanctionedHostRoots and its pending row leaves, so pending is empty.
 *   That file is the black-box conformance suite of the Python session host;
 *   its one loopback listener is the fake upstream that produces oversized,
 *   malformed and dropped replies, which the Contract Mock cannot. It serves no
 *   page and no browser reaches it. Adding an exemption is the change
 *   REVIEWED_SCANNER_SHA256 exists to clear, so the pin is the digest of this
 *   configuration; it is valid for this change only, and the next change to
 *   the scanner configuration or this script must reset it to null.
 * - 2026-11-22 (owner decision, Option A under KP-063 precedent):
 *   gui/tests/acceptance/support/ipc-proxy.mjs joins sanctionedHostRoots as
 *   the minimal ~113-line test-only ephemeral port + AF_UNIX drop-proxy helper
 *   for the S4 browser acceptance split; both former browser-acceptance.mjs
 *   ledger rows leave, shrinking allowlist 7 -> 5 (pending stays empty).
 *   REVIEWED_SCANNER_SHA256 is updated to the digest of this configuration;
 *   it is valid for this change only, and the next change to the scanner
 *   configuration or this script must reset it to null.
 */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ts = await import('../../gui/node_modules/typescript/lib/typescript.js');
const DEFAULT_ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)));
export const CONFIG_RELATIVE_PATH = 'scripts/ci/gui-request-paths-scanner.json';
export const FIRST_SEED_LEDGER_SHA256 = 'a0bd422b1300d88c8d8a9a196a82c7fae3215ef92d7f638554a028353d9d7b6a';
export const REVIEWED_SCANNER_SHA256 = '1c2947c31d5363021d8c045b7700ae4939fd1870b7820984e314c0e676f8f78e';
const DEFAULT_CONFIG = JSON.parse(readFileSync(new URL('./gui-request-paths-scanner.json', import.meta.url), 'utf8'));
export const LEDGER_RELATIVE_PATH = DEFAULT_CONFIG.ledgerPath;
const FIRST_SEED_BASES = new Set(['dev', 'main']);
const FINDING_KINDS = new Set([
  'network-primitive', 'url-literal', 'transport-construction', 'http-client-import', 'http-client-dependency',
  'host-route-handler', 'host-http-server', 'browser-route-fulfill', 'host-path-literal',
]);
const GROWABLE_KEYS = new Set([
  'extensions', 'htmlExtensions', 'sourceRoots', 'htmlRoots', 'htmlFiles', 'packageFiles', 'hostRoots', 'hostConfigRoots',
  'hostFilePatterns', 'networkNames', 'globalObjects', 'requestPathPatterns', 'absoluteUrlPatterns', 'hostPrivatePathPatterns',
  'bannedPrefixes', 'httpClientLibraries', 'hostServerLibraries', 'nodeServerModules', 'nodeServerFactories', 'viteModules', 'routeHooks',
]);
const SHRINKABLE_KEYS = new Set([
  'transportFiles', 'sanctionedHostRoots', 'reservedHostPatterns', 'skippedHostDirectories', 'skippedSourceDirectories', 'skippedFiles',
]);
const EXACT_KEYS = new Set(['version', 'ledgerPath', 'transportConstructionFile']);
const PATTERN_KEYS = ['hostFilePatterns', 'requestPathPatterns', 'absoluteUrlPatterns', 'reservedHostPatterns', 'hostPrivatePathPatterns'];

function git(root, ...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

function blobAt(root, ref, path) {
  try {
    git(root, 'cat-file', '-e', `${ref}:${path}`);
  } catch {
    return null;
  }
  return git(root, 'show', `${ref}:${path}`);
}

export function parseScannerConfig(value, label = 'scanner configuration') {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} is not an object`);
  const known = new Set([...GROWABLE_KEYS, ...SHRINKABLE_KEYS, ...EXACT_KEYS]);
  const unknown = Object.keys(value).filter(name => !known.has(name));
  if (unknown.length) throw new Error(`${label} has unknown keys: ${unknown.sort().join(',')}`);
  for (const name of known) {
    if (!(name in value)) throw new Error(`${label} is missing key ${name}`);
    const entry = value[name];
    if (name === 'version') {
      if (entry !== 1) throw new Error(`${label} version must be 1`);
    } else if (EXACT_KEYS.has(name)) {
      if (typeof entry !== 'string' || !entry) throw new Error(`${label} key ${name} must be a non-empty string`);
    } else if (!Array.isArray(entry) || entry.some(item => typeof item !== 'string' || !item)) {
      throw new Error(`${label} key ${name} must be a list of non-empty strings`);
    }
  }
  for (const name of PATTERN_KEYS) for (const source of value[name]) new RegExp(source);
  return value;
}

export function scannerDigest(config) {
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(config).sort(([left], [right]) => (left < right ? -1 : 1))));
  return createHash('sha256').update(canonical).digest('hex');
}

export function compareScannerConfigs(base, head) {
  const violations = [];
  const fail = (kind, detail) => violations.push({ kind, ...detail });
  for (const name of Object.keys(base)) {
    const before = base[name];
    const after = head[name];
    if (after === undefined) fail('scanner-key-removed', { name });
    else if (GROWABLE_KEYS.has(name)) {
      const removed = before.filter(item => !after.includes(item));
      if (removed.length) fail('scanner-narrowed', { name, removed });
    } else if (SHRINKABLE_KEYS.has(name)) {
      const added = after.filter(item => !before.includes(item));
      if (added.length) fail('scanner-exclusion-added', { name, added });
    } else if (JSON.stringify(before) !== JSON.stringify(after)) {
      fail('scanner-constant-changed', { name, base: before, head: after });
    }
  }
  for (const name of Object.keys(head)) if (base[name] === undefined) fail('scanner-key-unknown', { name });
  return violations;
}

function context(config) {
  const set = name => new Set(config[name]);
  const patterns = name => config[name].map(source => new RegExp(source));
  return {
    config,
    extensions: set('extensions'),
    htmlExtensions: set('htmlExtensions'),
    networkNames: set('networkNames'),
    globalObjects: set('globalObjects'),
    routeHooks: set('routeHooks'),
    nodeServerModules: set('nodeServerModules'),
    nodeServerFactories: set('nodeServerFactories'),
    viteModules: set('viteModules'),
    transportFiles: set('transportFiles'),
    skippedHostDirectories: set('skippedHostDirectories'),
    skippedSourceDirectories: set('skippedSourceDirectories'),
    skippedFiles: set('skippedFiles'),
    requestPath: patterns('requestPathPatterns'),
    absoluteUrl: patterns('absoluteUrlPatterns'),
    reservedHost: patterns('reservedHostPatterns'),
    hostPrivate: patterns('hostPrivatePathPatterns'),
    hostFile: patterns('hostFilePatterns'),
  };
}

function key(row) {
  return `${row.kind}\0${row.path}\0${row.subject}`;
}

function rows(findings) {
  const counts = new Map();
  for (const finding of findings) counts.set(key(finding), (counts.get(key(finding)) ?? 0) + 1);
  return [...counts.entries()].sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)).map(([entry, count]) => {
    const [kind, path, subject] = entry.split('\0');
    return { kind, path, subject, count };
  });
}

function walk(root, prefix, accept, skippedDirectories) {
  const result = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true }).sort((left, right) => (left.name < right.name ? -1 : 1))) {
      if (entry.isDirectory()) {
        if (!skippedDirectories.has(entry.name)) visit(join(directory, entry.name));
      } else if (accept(join(directory, entry.name))) {
        result.push(join(directory, entry.name));
      }
    }
  };
  const directory = join(root, prefix);
  if (existsSync(directory)) visit(directory);
  return result;
}

function relativeName(root, path) {
  return relative(root, path).replaceAll('\\', '/');
}

function parseText(name, text) {
  const kind = /x$/.test(name) ? ts.ScriptKind.TSX : /\.[cm]?js$/.test(name) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
  return ts.createSourceFile(name, text, ts.ScriptTarget.ESNext, true, kind);
}

function literalPieces(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [node.text];
  if (ts.isTemplateExpression(node)) return [node.head.text, ...node.templateSpans.map(span => span.literal.text)];
  return null;
}

function stringLike(node) {
  return node !== undefined && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node));
}

function unwrap(node) {
  let current = node;
  while (ts.isParenthesizedExpression(current) || ts.isNonNullExpression(current) || ts.isAsExpression(current)
      || ts.isSatisfiesExpression(current) || ts.isTypeAssertionExpression(current)) {
    current = current.expression;
  }
  return current;
}

function memberName(node) {
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && stringLike(node.argumentExpression)) return node.argumentExpression.text;
  return null;
}

function globalChain(node, globals) {
  const current = unwrap(node);
  if (ts.isIdentifier(current)) return globals.has(current.text) ? current.text : null;
  const name = memberName(current);
  if (name === null || !globals.has(name)) return null;
  const inner = globalChain(current.expression, globals);
  return inner === null ? null : `${inner}.${name}`;
}

function propertyKey(element) {
  const name = element.propertyName ?? element.name;
  return ts.isIdentifier(name) || stringLike(name) ? name.text : null;
}

function isDeclarationName(node) {
  const parent = node.parent;
  if (!parent) return false;
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) return true;
  if (ts.isQualifiedName(parent) && parent.right === node) return true;
  if ((ts.isPropertyAssignment(parent) || ts.isPropertySignature(parent) || ts.isPropertyDeclaration(parent)
      || ts.isMethodDeclaration(parent) || ts.isMethodSignature(parent) || ts.isGetAccessorDeclaration(parent)
      || ts.isSetAccessorDeclaration(parent) || ts.isEnumMember(parent)) && parent.name === node) return true;
  if (ts.isBindingElement(parent) && parent.propertyName === node) return true;
  if (ts.isJsxAttribute(parent) && parent.name === node) return true;
  return false;
}

function isValueReference(node) {
  const parent = node.parent;
  if (isDeclarationName(node)) return false;
  if (ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent) || ts.isTypeReferenceNode(parent)) return false;
  if (ts.isBindingElement(parent) && parent.name === node) return false;
  if (ts.isVariableDeclaration(parent) && parent.name === node) return false;
  if (ts.isParameter(parent) && parent.name === node) return false;
  return true;
}

function calleeName(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  return null;
}

function moduleSpecifierOf(node) {
  if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
    return node.moduleSpecifier.text;
  }
  if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && stringLike(node.moduleReference.expression)) {
    return node.moduleReference.expression.text;
  }
  if (ts.isCallExpression(node) && stringLike(node.arguments[0])
      && (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === 'require'))) {
    return node.arguments[0].text;
  }
  return null;
}

function libraryOf(specifier, names) {
  const bare = specifier.replace(/^node:/, '');
  return names.find(name => bare === name || bare.startsWith(`${name}/`)) ?? null;
}

function scanLiterals(node, name, ctx, findings, { kind, exempt, matchesPath }) {
  const pieces = literalPieces(node);
  if (pieces === null) return;
  pieces.forEach((piece, index) => {
    const direct = index === 0 && !exempt && matchesPath(piece);
    if (direct || ctx.config.bannedPrefixes.some(prefix => piece.includes(prefix))) {
      findings.push({ kind, path: name, subject: piece.slice(0, 120) });
    }
  });
}

function scanSourceFile(name, source, ctx) {
  const findings = [];
  const transportFile = ctx.transportFiles.has(name);
  const matchesPath = text => ctx.requestPath.some(pattern => pattern.test(text))
    || (ctx.absoluteUrl.some(pattern => pattern.test(text)) && !ctx.reservedHost.some(pattern => pattern.test(text)));
  const network = subject => findings.push({ kind: 'network-primitive', path: name, subject });
  const visit = (node) => {
    const specifier = moduleSpecifierOf(node);
    const library = specifier === null ? null : libraryOf(specifier, ctx.config.httpClientLibraries);
    if (library !== null) findings.push({ kind: 'http-client-import', path: name, subject: library });
    if (!transportFile) {
      if (ts.isIdentifier(node) && ctx.networkNames.has(node.text) && !isDeclarationName(node)) network(node.text);
      const member = memberName(node);
      if (member !== null && ctx.networkNames.has(member)) {
        const chain = globalChain(node.expression, ctx.globalObjects);
        if (chain !== null) network(`${chain}.${member}`);
      }
      if (ts.isBindingElement(node) && node.propertyName && ts.isObjectBindingPattern(node.parent)) {
        const property = propertyKey(node);
        const declaration = node.parent.parent;
        const chain = ts.isVariableDeclaration(declaration) && declaration.initializer ? globalChain(declaration.initializer, ctx.globalObjects) : null;
        if (chain !== null && ctx.networkNames.has(property)) network(`${chain}.${property}`);
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isObjectLiteralExpression(node.left)) {
        const chain = globalChain(node.right, ctx.globalObjects);
        for (const property of chain === null ? [] : node.left.properties) {
          const text = ts.isPropertyAssignment(property) && (ts.isIdentifier(property.name) || stringLike(property.name)) ? property.name.text : null;
          if (text !== null && ctx.networkNames.has(text)) network(`${chain}.${text}`);
        }
      }
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'get'
          && ts.isIdentifier(node.expression.expression) && node.expression.expression.text === 'Reflect' && node.arguments.length >= 2) {
        const chain = globalChain(node.arguments[0], ctx.globalObjects);
        if (chain !== null && stringLike(node.arguments[1]) && ctx.networkNames.has(node.arguments[1].text)) network(`${chain}.${node.arguments[1].text}`);
      }
    }
    scanLiterals(node, name, ctx, findings, { kind: 'url-literal', exempt: transportFile, matchesPath });
    if (ts.isNewExpression(node) && calleeName(node.expression) === 'HttpTransport' && name !== ctx.config.transportConstructionFile) {
      findings.push({ kind: 'transport-construction', path: name, subject: 'HttpTransport' });
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(source, visit);
  return findings;
}

function scanHtml(root, path, ctx) {
  const name = relativeName(root, path);
  const text = readFileSync(path, 'utf8');
  const findings = [];
  const scripts = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
  for (const match of text.matchAll(scripts)) {
    const type = /\btype\s*=\s*["']?([^"'\s>]*)/i.exec(match[1])?.[1]?.toLowerCase();
    if (/\bsrc\s*=/i.test(match[1]) || (type && !['module', 'text/javascript', 'application/javascript'].includes(type))) continue;
    findings.push(...scanSourceFile(name, parseText(`${name}.js`, match[2]), ctx));
  }
  const markup = text.replace(scripts, '<script></script>');
  for (const prefix of ctx.config.bannedPrefixes) {
    for (let count = markup.split(prefix).length - 1; count > 0; count -= 1) findings.push({ kind: 'url-literal', path: name, subject: prefix });
  }
  return findings;
}

function scanPackage(root, file, ctx) {
  const path = join(root, file);
  if (!existsSync(path)) return [];
  const document = JSON.parse(readFileSync(path, 'utf8'));
  const findings = [];
  for (const section of ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies']) {
    for (const dependency of Object.keys(document[section] ?? {})) {
      if (ctx.config.httpClientLibraries.includes(dependency)) findings.push({ kind: 'http-client-dependency', path: file, subject: dependency });
      if (ctx.config.hostServerLibraries.includes(dependency)) findings.push({ kind: 'host-http-server', path: file, subject: dependency });
    }
  }
  return findings;
}

function enclosingName(node) {
  for (let current = node.parent; current; current = current.parent) {
    if ((ts.isFunctionDeclaration(current) || ts.isMethodDeclaration(current)) && current.name) return current.name.getText();
    if ((ts.isArrowFunction(current) || ts.isFunctionExpression(current)) && ts.isVariableDeclaration(current.parent) && ts.isIdentifier(current.parent.name)) {
      return current.parent.name.text;
    }
  }
  return '<module>';
}

function kindOfModule(specifier, ctx) {
  const bare = specifier.replace(/^node:/, '');
  if (ctx.nodeServerModules.has(bare)) return 'node';
  if (ctx.viteModules.has(bare)) return 'vite';
  return null;
}

function moduleKind(expression, ctx, modules) {
  let current = unwrap(expression);
  if (ts.isAwaitExpression(current)) current = unwrap(current.expression);
  if (memberName(current) === 'default') return moduleKind(current.expression, ctx, modules);
  if (ts.isIdentifier(current)) return modules.get(current.text) ?? null;
  const specifier = ts.isCallExpression(current) ? moduleSpecifierOf(current) : null;
  return specifier === null ? null : kindOfModule(specifier, ctx);
}

function registerFactory(factories, ctx, kind, imported, local) {
  if ((kind === 'node' && ctx.nodeServerFactories.has(imported)) || (kind === 'vite' && imported === 'createServer')) {
    factories.set(local, { kind, name: imported });
  }
}

function collectBindings(source, ctx) {
  const state = { modules: new Map(), factories: new Map(), middlewares: new Set(), uses: new Set() };
  const isMiddlewares = (node) => {
    const current = unwrap(node);
    return memberName(current) === 'middlewares' || (ts.isIdentifier(current) && state.middlewares.has(current.text));
  };
  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier) && node.importClause) {
      const kind = kindOfModule(node.moduleSpecifier.text, ctx);
      const clause = node.importClause;
      if (kind !== null) {
        if (clause.name) state.modules.set(clause.name.text, kind);
        if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) state.modules.set(clause.namedBindings.name.text, kind);
        else if (clause.namedBindings) {
          for (const element of clause.namedBindings.elements) registerFactory(state.factories, ctx, kind, (element.propertyName ?? element.name).text, element.name.text);
        }
      }
    }
    if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && stringLike(node.moduleReference.expression)) {
      const kind = kindOfModule(node.moduleReference.expression.text, ctx);
      if (kind !== null) state.modules.set(node.name.text, kind);
    }
    if (ts.isVariableDeclaration(node) && node.initializer) {
      const init = unwrap(node.initializer);
      const kind = moduleKind(init, ctx, state.modules);
      const member = memberName(init);
      if (ts.isIdentifier(node.name)) {
        if (kind !== null) state.modules.set(node.name.text, kind);
        if (member !== null) {
          const owner = moduleKind(init.expression, ctx, state.modules);
          if (owner !== null) registerFactory(state.factories, ctx, owner, member, node.name.text);
          if (member === 'middlewares') state.middlewares.add(node.name.text);
          if (member === 'use' && isMiddlewares(init.expression)) state.uses.add(node.name.text);
        }
        if (ts.isIdentifier(init) && state.factories.has(init.text)) state.factories.set(node.name.text, state.factories.get(init.text));
      } else if (ts.isObjectBindingPattern(node.name)) {
        for (const element of node.name.elements) {
          const property = propertyKey(element);
          if (!ts.isIdentifier(element.name) || property === null) continue;
          if (kind !== null) registerFactory(state.factories, ctx, kind, property, element.name.text);
          if (property === 'middlewares') state.middlewares.add(element.name.text);
          if (property === 'use' && isMiddlewares(init)) state.uses.add(element.name.text);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  for (let pass = 0; pass < 2; pass += 1) ts.forEachChild(source, visit);
  return state;
}

function hasHostKey(node, names) {
  let found = false;
  const visit = (child) => {
    if ((ts.isPropertyAssignment(child) || ts.isShorthandPropertyAssignment(child) || ts.isMethodDeclaration(child))
        && (ts.isIdentifier(child.name) || stringLike(child.name)) && names.has(child.name.text)) found = true;
    ts.forEachChild(child, visit);
  };
  visit(node);
  return found;
}

function scanHost(root, path, ctx) {
  const name = relativeName(root, path);
  const source = parseText(name, readFileSync(path, 'utf8'));
  const sanctioned = ctx.config.sanctionedHostRoots.some(prefix => name.startsWith(prefix));
  const findings = [];
  const state = sanctioned ? null : collectBindings(source, ctx);
  const matchesPath = text => ctx.hostPrivate.some(pattern => pattern.test(text));
  const server = (node, subject) => findings.push({ kind: 'host-http-server', path: name, subject: `${subject}@${enclosingName(node)}` });
  const fulfill = () => findings.push({ kind: 'browser-route-fulfill', path: name, subject: 'fulfill' });
  const viteServer = (reference) => {
    const call = ts.isCallExpression(reference.parent) && reference.parent.expression === reference ? reference.parent : null;
    if (call !== null && call.arguments.length === 0) return;
    const argument = call?.arguments[0];
    if (argument === undefined || !ts.isObjectLiteralExpression(argument) || hasHostKey(argument, new Set(['plugins', 'configureServer']))) {
      server(reference, 'vite.createServer');
    }
  };
  const visit = (node) => {
    scanLiterals(node, name, ctx, findings, { kind: 'host-path-literal', exempt: sanctioned, matchesPath });
    if (!sanctioned) {
      const specifier = moduleSpecifierOf(node);
      const library = specifier === null ? null : libraryOf(specifier, ctx.config.hostServerLibraries);
      if (library !== null) findings.push({ kind: 'host-http-server', path: name, subject: library });
      if ((ts.isMethodDeclaration(node) || ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node))
          && (ts.isIdentifier(node.name) || stringLike(node.name)) && ctx.routeHooks.has(node.name.text)) {
        findings.push({ kind: 'host-route-handler', path: name, subject: node.name.text });
      }
      if (ts.isCallExpression(node)) {
        const callee = unwrap(node.expression);
        const target = memberName(callee) === 'use' ? unwrap(callee.expression) : null;
        const viaAlias = target !== null && ts.isIdentifier(target) && state.middlewares.has(target.text);
        if ((target !== null && memberName(target) === 'middlewares') || viaAlias || (ts.isIdentifier(callee) && state.uses.has(callee.text))) {
          findings.push({ kind: 'host-route-handler', path: name, subject: 'middlewares.use' });
        }
      }
      const member = memberName(node);
      if (member !== null) {
        const kind = moduleKind(node.expression, ctx, state.modules);
        if (kind === 'node' && ctx.nodeServerFactories.has(member)) server(node, member);
        if (kind === 'vite' && member === 'createServer') viteServer(node);
        if (member === 'fulfill') fulfill();
      }
      if (ts.isBindingElement(node) && propertyKey(node) === 'fulfill') fulfill();
      if (ts.isIdentifier(node) && state.factories.has(node.text) && isValueReference(node)) {
        const factory = state.factories.get(node.text);
        if (factory.kind === 'node') server(node, factory.name);
        else viteServer(node);
      }
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(source, visit);
  return findings;
}

export function scan(root, config = DEFAULT_CONFIG) {
  const resolved = resolve(root);
  const ctx = context(config);
  const findings = [];
  const keep = path => !ctx.skippedFiles.has(relativeName(resolved, path));
  const extensionOf = path => path.slice(path.lastIndexOf('.'));
  const code = path => ctx.extensions.has(extensionOf(path));
  const markup = path => ctx.htmlExtensions.has(extensionOf(path));
  const hostConfig = path => ctx.hostFile.some(pattern => pattern.test(relativeName(resolved, path)));
  for (const prefix of config.sourceRoots) {
    for (const path of walk(resolved, prefix, code, ctx.skippedSourceDirectories).filter(keep)) {
      const name = relativeName(resolved, path);
      findings.push(...scanSourceFile(name, parseText(name, readFileSync(path, 'utf8')), ctx));
    }
  }
  const htmlPaths = new Set(config.htmlFiles.map(file => join(resolved, file)).filter(existsSync));
  for (const prefix of config.htmlRoots) for (const path of walk(resolved, prefix, markup, ctx.skippedSourceDirectories)) htmlPaths.add(path);
  for (const path of [...htmlPaths].sort().filter(keep)) findings.push(...scanHtml(resolved, path, ctx));
  for (const file of config.packageFiles) findings.push(...scanPackage(resolved, file, ctx));
  const hostPaths = new Set();
  for (const prefix of config.hostRoots) for (const path of walk(resolved, prefix, code, ctx.skippedHostDirectories)) hostPaths.add(path);
  for (const prefix of config.hostConfigRoots) for (const path of walk(resolved, prefix, hostConfig, ctx.skippedHostDirectories)) hostPaths.add(path);
  for (const path of [...hostPaths].sort().filter(keep)) findings.push(...scanHost(resolved, path, ctx));
  return rows(findings);
}

const ROW_KEYS = { allowlist: ['count', 'kind', 'path', 'reason', 'subject'], pending: ['count', 'kind', 'path', 'subject'] };

function parseRows(rowsValue, list, label) {
  if (!Array.isArray(rowsValue)) throw new Error(`${label}: ${list} is not an array`);
  const result = new Map();
  for (const [index, row] of rowsValue.entries()) {
    const where = `${label}: ${list} row #${index}`;
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error(`${where} is not an object`);
    const keys = Object.keys(row).sort();
    if (keys.join() !== ROW_KEYS[list].join()) throw new Error(`${where} has unknown or missing keys: ${keys.join(',')}`);
    if (!FINDING_KINDS.has(row.kind)) throw new Error(`${where} has unknown finding kind: ${row.kind}`);
    if (![row.path, row.subject].every(value => typeof value === 'string' && value.length > 0)) throw new Error(`${where} has an invalid path or subject`);
    if (!Number.isInteger(row.count) || row.count <= 0) throw new Error(`${where} has an invalid count`);
    if (list === 'allowlist' && (typeof row.reason !== 'string' || row.reason.trim().length < 10)) throw new Error(`${where} has no reason`);
    if (result.has(key(row))) throw new Error(`${where} is a duplicate`);
    result.set(key(row), row.count);
  }
  return result;
}

export function parseLedger(document, label) {
  if (!document || document.version !== 1 || typeof document.policy !== 'string') throw new Error(`${label}: version/policy invalid`);
  const keys = Object.keys(document).sort();
  if (keys.join() !== 'allowlist,pending,policy,version') throw new Error(`${label}: unknown or missing top-level keys: ${keys.join(',')}`);
  return { allowlist: parseRows(document.allowlist, 'allowlist', label), pending: parseRows(document.pending, 'pending', label) };
}

export function _ledgerDocument(allowlist, pending) {
  return {
    version: 1,
    policy: 'Request-path exceptions of the GUI. allowlist rows are exceptions that stay for now and carry their own reason; pending rows are findings that later stages of PR 1e remove. Neither list may grow against the base branch: rows may only leave or shrink.',
    allowlist,
    pending,
  };
}

function describe(entry, extra) {
  const [kind, path, subject] = entry.split('\0');
  return { finding: kind, path, subject, ...extra };
}

function baseLedger(root, ref, ledgerRelativePath) {
  const text = blobAt(root, ref, ledgerRelativePath);
  return text === null ? null : parseLedger(JSON.parse(text), 'base ledger');
}

function baseScanner(root, ref) {
  const text = blobAt(root, ref, CONFIG_RELATIVE_PATH);
  if (text === null) return { config: null, problem: null };
  try {
    return { config: parseScannerConfig(JSON.parse(text), 'base scanner configuration'), problem: null };
  } catch (error) {
    return { config: null, problem: { kind: 'invalid-base-scanner-config', detail: error.message } };
  }
}

function compareScannerToBase(ref, baseScan, config, options) {
  let violations;
  if (baseScan.problem !== null) violations = [baseScan.problem];
  else if (baseScan.config === null) {
    violations = [{ kind: 'missing-base-scanner-config', detail: `${CONFIG_RELATIVE_PATH} is absent at ${ref} while the base has a ledger` }];
  } else violations = compareScannerConfigs(baseScan.config, config);
  const pinned = options.reviewedScannerSha256 === undefined ? REVIEWED_SCANNER_SHA256 : options.reviewedScannerSha256;
  if (violations.length && pinned !== null && pinned === scannerDigest(config)) return { violations: [], scannerOverride: true };
  return { violations, scannerOverride: false };
}

function compareToBase(root, ledgerPath, current, config, options) {
  const violations = [];
  const ref = options.baseRef;
  if (typeof ref !== 'string' || ref.length === 0) return { violations: [{ kind: 'invalid-base-ref', detail: 'empty base ref' }] };
  try {
    git(root, 'rev-parse', '--verify', `${ref}^{commit}`);
  } catch {
    return { violations: [{ kind: 'invalid-base-ref', detail: ref }] };
  }
  const baseScan = baseScanner(root, ref);
  let base;
  try {
    base = baseLedger(root, ref, baseScan.config?.ledgerPath ?? config.ledgerPath);
  } catch (error) {
    return { violations: [{ kind: 'invalid-base-ledger', detail: error.message }] };
  }
  if (base === null) {
    if (!options.allowFirstSeed) return { violations: [{ kind: 'missing-base-ledger', detail: `${config.ledgerPath} is absent at ${ref}` }] };
    if (!FIRST_SEED_BASES.has(options.baseBranch ?? '')) return { violations: [{ kind: 'missing-base-ledger', detail: 'first seed is limited to dev/main' }] };
    const digest = createHash('sha256').update(readFileSync(ledgerPath)).digest('hex');
    if (digest !== (options.firstSeedDigest ?? FIRST_SEED_LEDGER_SHA256)) return { violations: [{ kind: 'seed-digest-mismatch', path: config.ledgerPath }] };
    return { violations: [], firstSeed: true };
  }
  for (const list of ['allowlist', 'pending']) {
    for (const [entry, count] of current[list]) {
      if (count > (base[list].get(entry) ?? 0)) {
        violations.push({ kind: `${list}-ledger-growth`, ...describe(entry, { count, base: base[list].get(entry) ?? 0 }) });
      }
    }
  }
  const scanner = compareScannerToBase(ref, baseScan, config, options);
  violations.push(...scanner.violations);
  return { violations, scannerOverride: scanner.scannerOverride };
}

function loadConfig(root, options) {
  if (options.config !== undefined) return parseScannerConfig(options.config);
  const path = join(root, CONFIG_RELATIVE_PATH);
  return parseScannerConfig(existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : DEFAULT_CONFIG);
}

export function check(root, options = {}) {
  const resolved = resolve(root);
  let config;
  try {
    config = loadConfig(resolved, options);
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'invalid-scanner-config', detail: error.message }] };
  }
  const ledgerPath = options.ledger ?? join(resolved, config.ledgerPath);
  let parsed;
  try {
    parsed = parseLedger(JSON.parse(readFileSync(ledgerPath, 'utf8')), 'ledger');
  } catch (error) {
    return { status: 'FAIL', violations: [{ kind: 'invalid-ledger', detail: error.message }] };
  }
  const { allowlist: allowed, pending } = parsed;
  const violations = [];
  const current = new Map(scan(resolved, config).map(row => [key(row), row.count]));
  for (const [entry, count] of allowed) {
    if ((current.get(entry) ?? 0) !== count) {
      violations.push({ kind: 'stale-allowlist-entry', ...describe(entry, { count: current.get(entry) ?? 0, allowed: count }) });
    }
    if (pending.has(entry)) violations.push({ kind: 'allowlisted-and-pending', ...describe(entry, {}) });
  }
  for (const [entry, count] of current) {
    if (allowed.has(entry)) continue;
    const expected = pending.get(entry);
    if (expected === undefined) violations.push({ kind: 'new-request-path-finding', ...describe(entry, { count }) });
    else if (count > expected) violations.push({ kind: 'request-path-finding-grew', ...describe(entry, { count, pending: expected }) });
    else if (count < expected) violations.push({ kind: 'stale-pending-entry', ...describe(entry, { count, pending: expected }) });
  }
  for (const [entry, expected] of pending) {
    if (!current.has(entry)) violations.push({ kind: 'stale-pending-entry', ...describe(entry, { count: 0, pending: expected }) });
  }
  const against = options.baseRef === undefined ? { violations: [] } : compareToBase(resolved, ledgerPath, parsed, config, options);
  violations.push(...against.violations);
  return {
    status: violations.length ? 'FAIL' : 'PASS',
    baseRef: options.baseRef ?? null,
    firstSeed: against.firstSeed ?? false,
    scannerOverride: against.scannerOverride ?? false,
    violations,
    findings: current.size,
    allowlist: allowed.size,
    pending: pending.size,
  };
}

function writePending(root, ledgerPath, config) {
  const previousDocument = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  const previous = parseLedger(previousDocument, 'ledger');
  const allowed = new Set(previous.allowlist.keys());
  const next = scan(root, config).filter(row => !allowed.has(key(row)));
  const grown = next.filter(row => row.count > (previous.pending.get(key(row)) ?? 0));
  if (grown.length) {
    throw new Error(`refusing to grow the pending list: ${grown.map(row => `${row.kind} ${row.path} ${row.subject}`).join('; ')}`);
  }
  writeFileSync(ledgerPath, `${JSON.stringify(_ledgerDocument(previousDocument.allowlist, next), null, 2)}\n`);
  return next.length;
}

function main(argv) {
  const options = { root: DEFAULT_ROOT, json: false, write: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--root') options.root = resolve(argv[++index]);
    else if (argument === '--ledger') options.ledger = resolve(argv[++index]);
    else if (argument === '--base-ref') options.baseRef = argv[++index] ?? '';
    else if (argument === '--base-branch') options.baseBranch = argv[++index];
    else if (argument === '--allow-first-seed') options.allowFirstSeed = true;
    else if (argument === '--json') options.json = true;
    else if (argument === '--write-pending') options.write = true;
    else if (argument === '--print-scanner-digest') options.printDigest = true;
    else throw new Error(`unknown argument: ${argument}`);
  }
  const config = loadConfig(options.root, {});
  if (options.printDigest) {
    console.log(scannerDigest(config));
    return 0;
  }
  const ledgerPath = options.ledger ?? join(options.root, config.ledgerPath);
  if (options.write) {
    const count = writePending(options.root, ledgerPath, config);
    console.log(`wrote ${count} pending row(s) to ${relative(options.root, ledgerPath)}`);
    return 0;
  }
  const result = check(options.root, { ledger: ledgerPath, baseRef: options.baseRef, baseBranch: options.baseBranch, allowFirstSeed: options.allowFirstSeed });
  if (options.json) console.log(JSON.stringify(result, null, 2));
  else {
    for (const violation of result.violations) console.error(`FAIL ${JSON.stringify(violation)}`);
    const note = (result.firstSeed ? ' (first seed: the base has no ledger)' : '') + (result.scannerOverride ? ' (scanner change cleared by REVIEWED_SCANNER_SHA256)' : '');
    console.log(`${result.status} GUI request paths${note} (findings=${result.findings} allowlist=${result.allowlist ?? 'n/a'} pending=${result.pending ?? 'n/a'})`);
  }
  return result.status === 'PASS' ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`FAIL ${error.message}`);
    process.exitCode = 1;
  }
}
