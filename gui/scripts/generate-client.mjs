#!/usr/bin/env node
/**
 * Deterministic TypeScript generator for the Hyprial HTTP request surface.
 *
 * The input is the served HTTP OpenAPI projection, not the business MethodSpec
 * OpenAPI: method membership comes from `POST /daemon/request/<method>` paths
 * (optionally carrying `x-hyprial-method`), and each 200 response wraps the
 * declared business result in the transport success envelope.  Nothing here
 * knows the five Agent method names: add a path and the grouped entry grows.
 */
import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const GENERATOR_VERSION = 'h02-1';
const METHOD_PATTERN = /^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$/;
const METHOD_PATH = /^\/daemon\/request\/([A-Za-z0-9._-]+)$/;
const RETRY_POLICIES = new Set(['never', 'safe']);
const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
const UNSAFE_KEYS = new Set(['__proto__', 'prototype', 'constructor']);

export class GeneratorError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.name = 'GeneratorError';
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function fail(code, message) {
  throw new GeneratorError(code, message);
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function digestOf(document) {
  return `sha256:${createHash('sha256').update(canonicalJson(document)).digest('hex')}`;
}

function stableJson(value) {
  return JSON.stringify(JSON.parse(canonicalJson(value)), null, 2);
}

export function unwrapOpenApi(document) {
  if (!isRecord(document)) fail('NOT_OPENAPI', 'input is not an OpenAPI object');
  if (typeof document.openapi === 'string') return document;
  if (isRecord(document.result) && typeof document.result.openapi === 'string') return document.result;
  fail('NOT_OPENAPI', 'input has neither `openapi` nor a transport-wrapped `result.openapi`');
}

function lookupRef(document, ref) {
  if (!ref.startsWith('#/')) return undefined;
  let current = document;
  for (const rawSegment of ref.slice(2).split('/')) {
    const segment = rawSegment.replaceAll('~1', '/').replaceAll('~0', '~');
    if (!isRecord(current) || !(segment in current)) return undefined;
    current = current[segment];
  }
  return current;
}

function resolveNode(document, node) {
  let current = node;
  for (let depth = 0; depth < 32; depth += 1) {
    if (isRecord(current) && typeof current.$ref === 'string') {
      const target = lookupRef(document, current.$ref);
      if (target === undefined) fail('UNRESOLVED_REF', current.$ref);
      current = target;
      continue;
    }
    return current;
  }
  fail('REF_LOOP', 'reference chain is too deep');
}

function findResultSchema(document, node, depth = 0) {
  if (depth > 16) return undefined;
  const resolved = resolveNode(document, node);
  if (!isRecord(resolved)) return undefined;
  if (isRecord(resolved.properties) && 'result' in resolved.properties) return resolved.properties.result;
  if (Array.isArray(resolved.allOf)) {
    for (const branch of resolved.allOf) {
      const found = findResultSchema(document, branch, depth + 1);
      if (found !== undefined) return found;
    }
  }
  return undefined;
}

function pascal(value) {
  const cleaned = String(value).replace(/[^A-Za-z0-9]+(.)?/g, (_match, next) => (next ? next.toUpperCase() : ''));
  const name = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
  return name.length > 0 ? name : 'Method';
}

function camel(value) {
  const name = String(value).replace(/[-_]+(.)/g, (_match, next) => next.toUpperCase());
  return name.charAt(0).toLowerCase() + name.slice(1);
}

function methodSegments(name) {
  return name.split('.').map((segment) => segment.replace(/[-]/g, '_'));
}

function methodTypeBase(name) {
  return methodSegments(name).map(pascal).join('');
}

export function collectMethods(document) {
  const contract = unwrapOpenApi(document);
  if (!isRecord(contract.paths)) fail('NO_PATHS', 'OpenAPI document has no paths');

  const methods = [];
  const seen = new Set();
  for (const routePath of Object.keys(contract.paths).sort()) {
    const item = contract.paths[routePath];
    if (!isRecord(item) || !isRecord(item.post)) continue;
    const operation = item.post;
    const declared = operation['x-hyprial-method'];
    const match = METHOD_PATH.exec(routePath);
    if (match === null) {
      if (typeof declared === 'string') {
        fail('NON_CANONICAL_PATH', `${routePath} declares ${declared} outside /daemon/request/<method>`);
      }
      continue;
    }
    const name = typeof declared === 'string' ? declared : match[1];
    if (!METHOD_PATTERN.test(name)) fail('INVALID_METHOD', `${routePath} has a non-canonical method name ${name}`);
    if (declared !== undefined && declared !== match[1]) {
      fail('METHOD_PATH_MISMATCH', `${routePath} declares x-hyprial-method ${String(declared)}`);
    }
    if (seen.has(name)) fail('DUPLICATE_METHOD', `${name} is declared by more than one path`);
    seen.add(name);

    const paramsSchema = operation.requestBody?.content?.['application/json']?.schema;
    if (!isRecord(paramsSchema)) fail('MISSING_PARAMS_SCHEMA', `${name} has no application/json request schema`);
    const response = resolveNode(contract, operation.responses?.['200']);
    const successSchema = response?.content?.['application/json']?.schema;
    if (!isRecord(successSchema)) fail('MISSING_RESULT_SCHEMA', `${name} has no 200 application/json schema`);
    const resultSchema = findResultSchema(contract, successSchema);
    if (!isRecord(resultSchema)) {
      fail('MISSING_RESULT_SCHEMA', `${name} 200 schema has no transport envelope result`);
    }

    const mutates = operation['x-hyprial-mutates'] ?? false;
    if (typeof mutates !== 'boolean') fail('INVALID_METADATA', `${name} x-hyprial-mutates is not a boolean`);
    const retry = operation['x-hyprial-retry'] ?? 'never';
    if (!RETRY_POLICIES.has(retry)) fail('INVALID_RETRY', `${name} declares unsupported retry ${String(retry)}`);
    if (mutates && retry === 'safe') {
      fail('UNSAFE_RETRY', `${name} mutates but declares the safe read retry policy`);
    }
    const binding = isRecord(operation['x-hyprial-http-binding']) ? operation['x-hyprial-http-binding'] : undefined;
    const identity = operation['x-hyprial-identity'] ?? binding?.identity ?? { mode: 'none' };
    if (!isRecord(identity)) fail('INVALID_METADATA', `${name} identity metadata is not an object`);
    const errors = operation['x-hyprial-errors'] ?? [];
    if (!Array.isArray(errors)) fail('INVALID_METADATA', `${name} x-hyprial-errors is not an array`);
    if (!errors.every((entry) => isRecord(entry))) {
      fail('INVALID_METADATA', `${name} x-hyprial-errors entries must be objects`);
    }

    methods.push({
      name,
      path: routePath,
      state: typeof operation['x-hyprial-state'] === 'string' ? operation['x-hyprial-state'] : 'unknown',
      mutates,
      retry,
      identity,
      errors,
      paramsSchema,
      resultSchema,
    });
  }

  if (methods.length === 0) fail('NO_METHODS', 'no POST /daemon/request/<method> operations were found');
  methods.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));

  const identifiers = new Map();
  for (const method of methods) {
    const segments = method.name.split('.').map(camel);
    for (const segment of segments) {
      if (!IDENTIFIER.test(segment)) {
        fail('INVALID_IDENTIFIER', `${method.name} segment ${JSON.stringify(segment)} is not a TypeScript identifier`);
      }
      if (UNSAFE_KEYS.has(segment)) {
        fail('UNSAFE_PROPERTY_NAME', `${method.name} segment ${segment} is not allowed in a generated object`);
      }
    }
    const key = segments.join('.');
    const existing = identifiers.get(key);
    if (existing !== undefined && existing !== method.name) {
      fail('IDENTIFIER_COLLISION', `${existing} and ${method.name} both generate ${key}`);
    }
    identifiers.set(key, method.name);
  }
  for (const method of methods) {
    const segments = method.name.split('.').map(camel);
    for (let index = 1; index < segments.length; index += 1) {
      const prefix = segments.slice(0, index).join('.');
      if (identifiers.has(prefix)) {
        fail('NAMESPACE_COLLISION', `${prefix} is both a method and a namespace of ${method.name}`);
      }
    }
  }
  return methods;
}

function createTypeEmitter(document) {
  const declarations = new Map();
  const fingerprints = new Map();
  const refNames = new Map();

  function refName(ref) {
    const existing = refNames.get(ref);
    if (existing !== undefined) return existing;
    const target = lookupRef(document, ref);
    if (target === undefined) fail('UNRESOLVED_REF', ref);
    const baseName = pascal(ref.split('/').at(-1));
    let name = baseName;
    let suffix = 2;
    const fingerprint = canonicalJson(target);
    while (fingerprints.has(name) && fingerprints.get(name) !== fingerprint) {
      name = `${baseName}${suffix}`;
      suffix += 1;
    }
    refNames.set(ref, name);
    declare(name, target);
    return name;
  }

  function declare(name, schema) {
    const fingerprint = canonicalJson(schema);
    if (fingerprints.has(name)) {
      if (fingerprints.get(name) !== fingerprint) fail('TYPENAME_COLLISION', `two schemas want the type name ${name}`);
      return;
    }
    fingerprints.set(name, fingerprint);
    if (isRecord(schema) && schema.type === 'object' && isRecord(schema.properties) && Object.keys(schema.properties).length > 0) {
      declarations.set(name, renderInterface(name, schema));
    } else {
      declarations.set(name, `export type ${name} = ${emit(schema, name)};`);
    }
  }

  function objectIndexUnion(indexType, entries) {
    const named = entries.map((entry) => entry.type);
    if (entries.some((entry) => entry.optional === '?')) named.push('undefined');
    const extraParts = /[{}]/.test(indexType) ? [indexType] : indexType.split(' | ');
    return union([...extraParts, ...named]);
  }

  function objectIndexType(schema, hint) {
    const extra = schema.additionalProperties;
    if (extra === false) return undefined;
    if (extra === undefined || extra === true) return 'unknown';
    if (isRecord(extra)) return emit(extra, `${hint}Value`);
    fail('INVALID_ADDITIONAL_PROPERTIES', `${hint} additionalProperties must be a boolean or a schema`);
  }

  function renderInterface(name, schema) {
    const required = new Set(Array.isArray(schema.required) ? schema.required : []);
    const properties = schema.properties;
    const entries = Object.keys(properties)
      .sort()
      .map((key) => ({
        key,
        optional: required.has(key) ? '' : '?',
        type: emit(properties[key], `${name}${pascal(key)}`),
      }));
    const lines = entries.map(
      (entry) => `  ${IDENTIFIER.test(entry.key) ? entry.key : JSON.stringify(entry.key)}${entry.optional}: ${entry.type};`,
    );
    const indexType = objectIndexType(schema, name);
    if (indexType !== undefined) {
      const index = indexType === 'unknown' ? 'unknown' : objectIndexUnion(indexType, entries);
      lines.push(`  [key: string]: ${index};`);
    }
    return `export interface ${name} {\n${lines.join('\n')}\n}`;
  }

  function emit(schema, hint) {
    if (schema === true || schema === undefined) return 'unknown';
    if (schema === false) return 'never';
    if (!isRecord(schema)) return 'unknown';
    if (typeof schema.$ref === 'string') return refName(schema.$ref);

    const parts = [];
    if ('const' in schema) parts.push(literal(schema.const));
    else if (Array.isArray(schema.enum)) {
      const values = schema.enum.map(literal).sort();
      parts.push(values.length === 0 ? 'never' : unique(values).join(' | '));
    } else if (Array.isArray(schema.anyOf) || Array.isArray(schema.oneOf)) {
      const branch = schema.anyOf ?? schema.oneOf;
      parts.push(union(branch.map((child, index) => emit(child, `${hint}Any${index}`))));
    } else if (Array.isArray(schema.allOf)) {
      parts.push(intersection(schema.allOf.map((child, index) => emit(child, `${hint}All${index}`))));
    } else if (Array.isArray(schema.type)) {
      parts.push(union(schema.type.map((type) => emitBase({ ...schema, type }, hint))));
    } else {
      parts.push(emitBase(schema, hint));
    }
    if (schema.nullable === true) parts.push('null');
    return union(parts);
  }

  function emitBase(schema, hint) {
    const type = schema.type;
    if (type === 'object' || (type === undefined && (isRecord(schema.properties) || 'additionalProperties' in schema))) {
      const properties = isRecord(schema.properties) ? schema.properties : undefined;
      if (properties !== undefined) {
        const required = new Set(Array.isArray(schema.required) ? schema.required : []);
        const entries = Object.keys(properties)
          .sort()
          .map((key) => ({
            optional: required.has(key) ? '' : '?',
            key: IDENTIFIER.test(key) ? key : JSON.stringify(key),
            type: emit(properties[key], `${hint}${pascal(key)}`),
          }));
        const indexType = objectIndexType(schema, hint);
        if (entries.length === 0) {
          if (indexType === undefined) return 'Record<string, never>';
          return indexType === 'unknown' ? 'Record<string, unknown>' : `Record<string, ${indexType}>`;
        }
        const members = entries.map((entry) => `${entry.key}${entry.optional}: ${entry.type}`);
        if (indexType !== undefined) {
          members.push(`[key: string]: ${indexType === 'unknown' ? 'unknown' : objectIndexUnion(indexType, entries)}`);
        }
        return `{ ${members.join('; ')} }`;
      }
      const indexType = objectIndexType(schema, hint);
      if (indexType === undefined) return 'Record<string, never>';
      return indexType === 'unknown' ? 'Record<string, unknown>' : `Record<string, ${indexType}>`;
    }
    if (type === 'array') return `Array<${emit(schema.items, `${hint}Item`)}>`;
    if (type === 'string') return 'string';
    if (type === 'number' || type === 'integer') return 'number';
    if (type === 'boolean') return 'boolean';
    if (type === 'null') return 'null';
    return 'unknown';
  }

  function literal(value) {
    if (typeof value === 'string') return JSON.stringify(value);
    if (typeof value === 'number' || typeof value === 'boolean' || value === null) return String(value);
    fail('INVALID_ENUM', `cannot render ${canonicalJson(value)} as a TypeScript literal`);
  }

  function unique(values) {
    return [...new Set(values)];
  }

  function union(values) {
    const flat = unique(values.filter((value) => value !== 'never'));
    if (flat.length === 0) return 'never';
    return flat.sort().join(' | ');
  }

  function intersection(values) {
    const flat = unique(values);
    if (flat.length === 0) return 'unknown';
    return flat.sort().join(' & ');
  }

  return { declare, emit, declarations };
}

function buildTree(methods) {
  const root = Object.create(null);
  for (const method of methods) {
    const segments = methodSegments(method.name).map(camel);
    let node = root;
    for (const segment of segments.slice(0, -1)) {
      node[segment] ??= Object.create(null);
      node = node[segment];
    }
    node[segments.at(-1)] = { method };
  }
  return root;
}

function renderTree(node, indent, lines) {
  for (const key of Object.keys(node).sort()) {
    const value = node[key];
    if (value.method !== undefined) {
      const method = value.method;
      const base = methodTypeBase(method.name);
      lines.push(
        `${indent}/** ${method.name}; state: ${method.state}; mutates: ${String(method.mutates)}; retry: ${method.retry}. */`,
        `${indent}${key}(params: ${base}Params, options?: RequestOptions) {`,
        `${indent}  return transport.invoke<${base}Result>('${method.name}', params, { ...options, retry: '${method.retry}' });`,
        `${indent}},`,
      );
    } else {
      lines.push(`${indent}${key}: {`);
      renderTree(value, `${indent}  `, lines);
      lines.push(`${indent}},`);
    }
  }
  return lines;
}

function renderTypes(contract, methods, digest, sourceLabel) {
  const emitter = createTypeEmitter(contract);
  for (const method of methods) {
    const base = methodTypeBase(method.name);
    emitter.declare(`${base}Params`, method.paramsSchema);
    emitter.declare(`${base}Result`, method.resultSchema);
  }
  const declarations = [...emitter.declarations.keys()]
    .sort()
    .map((name) => emitter.declarations.get(name));
  const metadata = {};
  for (const method of methods) {
    metadata[method.name] = {
      state: method.state,
      mutates: method.mutates,
      retry: method.retry,
      identity: method.identity,
      errors: method.errors,
    };
  }
  return [
    `// GENERATED by gui/scripts/generate-client.mjs (${GENERATOR_VERSION}). DO NOT EDIT.`,
    `// source: ${sourceLabel}`,
    `// digest: ${digest}`,
    `// openapi: ${contract.openapi}`,
    `// methods: ${methods.length}`,
    '',
    declarations.join('\n\n'),
    '',
    'export interface MethodMetadata {',
    '  readonly state: string;',
    '  readonly mutates: boolean;',
    "  readonly retry: 'never' | 'safe';",
    '  readonly identity: Readonly<Record<string, unknown>>;',
    '  readonly errors: ReadonlyArray<Readonly<Record<string, unknown>>>;',
    '}',
    '',
    'export const methodMetadata = {',
    ...Object.keys(metadata)
      .sort()
      .map((name) => {
        const value = stableJson(metadata[name]).replace(/\n/g, '\n  ');
        return `  '${name}': ${value},`;
      }),
    '} as const satisfies Readonly<Record<string, MethodMetadata>>;',
    '',
    `export type GeneratedMethod = keyof typeof methodMetadata;`,
  ]
    .join('\n')
    .replace(/\n{3,}/g, '\n\n');
}

function renderIndex(methods, digest, sourceLabel, transportImport) {
  const bases = [...new Set(methods.map((method) => methodTypeBase(method.name)))].sort();
  const typeImports = bases.flatMap((base) => [`${base}Params`, `${base}Result`]).join(',\n  ');
  const tree = renderTree(buildTree(methods), '    ', []);
  return [
    `// GENERATED by gui/scripts/generate-client.mjs (${GENERATOR_VERSION}). DO NOT EDIT.`,
    `// source: ${sourceLabel}`,
    `// digest: ${digest}`,
    `// openapi: 3.x HTTP projection; methods: ${methods.length}`,
    '',
    `import type { HttpTransport } from '${transportImport}'`,
    `import type { RequestOptions } from '${transportImport}'`,
    'import type {',
    `  ${typeImports},`,
    "} from './types';",
    '',
    "export { methodMetadata } from './types';",
    "export type { GeneratedMethod, MethodMetadata } from './types';",
    '',
    'export function createGeneratedClient(transport: Pick<HttpTransport, \'invoke\'>) {',
    '  return {',
    ...tree,
    '  };',
    '}',
    '',
    'export type GeneratedClient = ReturnType<typeof createGeneratedClient>;',
    '',
  ].join('\n');
}

export function renderClient(document, options = {}) {
  const contract = unwrapOpenApi(document);
  const methods = collectMethods(contract);
  const digest = digestOf(contract);
  const sourceLabel = options.source ?? 'OpenAPI document';
  const transportImport = options.transportImport ?? '..';
  return {
    methods,
    digest,
    files: {
      'types.ts': renderTypes(contract, methods, digest, sourceLabel),
      'index.ts': renderIndex(methods, digest, sourceLabel, transportImport),
    },
  };
}

function transportImportFor(outDir, transportDir) {
  const relative = path.relative(path.resolve(outDir), path.resolve(transportDir)).split(path.sep).join('/');
  return relative.startsWith('.') ? relative : `./${relative}`;
}

export async function generateToDirectory(document, outDir, options = {}) {
  const rendered = renderClient(document, {
    ...options,
    transportImport: options.transportImport ?? transportImportFor(outDir, options.transportDir ?? defaultTransportDir()),
  });
  await fs.mkdir(outDir, { recursive: true });
  const names = Object.keys(rendered.files).sort();
  for (const name of names) await fs.writeFile(path.join(outDir, name), rendered.files[name], 'utf8');
  return names;
}

export async function checkDirectory(document, outDir, options = {}) {
  const rendered = renderClient(document, {
    ...options,
    transportImport: options.transportImport ?? transportImportFor(outDir, options.transportDir ?? defaultTransportDir()),
  });
  const drifted = [];
  for (const name of Object.keys(rendered.files).sort()) {
    try {
      const current = await fs.readFile(path.join(outDir, name), 'utf8');
      if (current !== rendered.files[name]) drifted.push(name);
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
      drifted.push(name);
    }
  }
  return drifted;
}

function defaultTransportDir() {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/client');
}

function parseArgs(argv) {
  const options = { check: false, json: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const next = () => {
      const value = argv[(index += 1)];
      if (value === undefined) fail('INVALID_ARGUMENT', `${argument} needs a value`);
      return value;
    };
    if (argument === '--input' || argument === '-i') options.input = next();
    else if (argument === '--out' || argument === '-o') options.out = next();
    else if (argument === '--transport-dir') options.transportDir = next();
    else if (argument === '--source') options.source = next();
    else if (argument === '--check') options.check = true;
    else if (argument === '--json') options.json = true;
    else if (argument === '--help' || argument === '-h') options.help = true;
    else fail('INVALID_ARGUMENT', `unknown argument ${argument}`);
  }
  return options;
}

function usage() {
  return [
    'Usage: node gui/scripts/generate-client.mjs --input <openapi.json> --out <dir> [--check]',
    '',
    '  --input, -i       served or dump-based HTTP OpenAPI JSON (`-` reads stdin)',
    '  --out, -o         output directory (default gui/src/client/generated)',
    '  --transport-dir   directory containing transport.ts (default gui/src/client)',
    '  --source          source label recorded in the generated header',
    '  --check           compare instead of writing; exit 1 on drift',
    '  --json            print a machine-readable summary',
  ].join('\n');
}

async function readInput(file) {
  if (file === '-') {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
  return JSON.parse(await fs.readFile(file, 'utf8'));
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(`${usage()}\n`);
    return 0;
  }
  if (options.input === undefined) fail('INVALID_ARGUMENT', '--input is required');
  const document = await readInput(options.input);
  const outDir = options.out ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/client/generated');
  const renderOptions = {
    source: options.source ?? options.input,
    transportDir: options.transportDir,
  };
  if (options.check) {
    const drifted = await checkDirectory(document, outDir, renderOptions);
    if (options.json) {
      process.stdout.write(`${JSON.stringify({ ok: drifted.length === 0, drifted }, null, 2)}\n`);
    } else if (drifted.length > 0) {
      process.stderr.write(`client drift: ${drifted.join(', ')}\n`);
    } else {
      process.stdout.write('client generation is up to date\n');
    }
    return drifted.length === 0 ? 0 : 1;
  }
  const written = await generateToDirectory(document, outDir, renderOptions);
  const rendered = renderClient(document, renderOptions);
  if (options.json) {
    process.stdout.write(
      `${JSON.stringify({ ok: true, out: outDir, files: written, digest: rendered.digest, methods: rendered.methods.map((method) => method.name) }, null, 2)}\n`,
    );
  } else {
    process.stdout.write(`generated ${written.join(', ')} in ${outDir} (${rendered.methods.length} methods)\n`);
  }
  return 0;
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 2;
    });
}
