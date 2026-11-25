/**
 * Synthetic contract for Contract Mock tests.
 *
 * Decision record (1e-2, 2026-11-15)
 * - Ported from tests/daemon/contract_mock/conftest.py: two unrelated demo
 *   methods prove the generic example contract without reading the real
 *   catalog. `demo.get` adds a selector (`id`, spoken about by its result) and
 *   a caller-minted identity key to exercise case selection.
 * - The document has the shape `projectContract` reads from the real
 *   `openapi.json`; the envelope is the real pinned schema file.
 * - Nothing here reads a home, socket, credential store or daemon.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { projectContract, withDigest } from '../../contract-mock/index.ts';
import type { ContractTable } from '../../contract-mock/index.ts';

type Doc = Record<string, any>;

const root = fileURLToPath(new URL('../../../contract/daemon-request/', import.meta.url));

export const SENTINEL = 'example-not-a-credential';
export const SENTINEL_B64 = 'ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs';

export function realOpenapi(): Doc {
  return JSON.parse(readFileSync(`${root}openapi.json`, 'utf8'));
}

export function realEnvelope(): Doc {
  return JSON.parse(readFileSync(`${root}http-envelope.schema.json`, 'utf8'));
}

function operation(name: string, params: Doc, result: Doc, errors: Doc[], examples: Doc[], gaps: Doc = {}): Doc {
  return {
    operationId: name,
    requestBody: { content: { 'application/json': { schema: params } }, required: true },
    responses: {
      '200': { content: { 'application/json': { schema: result } }, description: `${name} result` },
      default: { content: { 'application/json': { schema: { oneOf: errors } } }, description: 'Domain error envelope' },
    },
    'x-hyprial-examples': examples,
    ...(Object.keys(gaps).length > 0 ? { 'x-hyprial-example-gaps': gaps } : {}),
    'x-hyprial-mutates': name === 'demo.write',
  };
}

const errorShape = (code: string, data?: Doc): Doc => ({
  type: 'object',
  properties: { code: { const: code }, message: { type: 'string' }, ...(data ? { data } : {}) },
  required: ['code', 'message'],
  additionalProperties: false,
});

export function syntheticOpenapi(): Doc {
  const list = operation(
    'demo.list',
    {
      type: 'object',
      properties: { filter: { type: ['string', 'null'] } },
      required: ['filter'],
      additionalProperties: false,
    },
    {
      type: 'object',
      properties: { ok: { const: true }, items: { type: 'array', items: { type: 'string' } } },
      required: ['ok', 'items'],
      additionalProperties: false,
    },
    [
      errorShape('INVALID_ARGUMENT'),
      errorShape('DAEMON_NOT_READY', {
        type: 'object',
        properties: { reason: { type: 'string' } },
        required: ['reason'],
        additionalProperties: false,
      }),
    ],
    [
      { name: 'all', kind: 'success', description: 'one matching item is present', params: { filter: null }, result: { ok: true, items: ['alpha'] } },
      { name: 'no-match', kind: 'empty', description: 'a valid filter that matches nothing', params: { filter: 'missing' }, result: { ok: true, items: [] }, reason: 'an empty match is a legal successful result' },
      { name: 'wrong-filter-type', kind: 'invalid-params', description: 'the filter type is rejected before dispatch', params: { filter: 5 }, error: { code: 'INVALID_ARGUMENT', message: 'demo.list params do not match the schema' } },
      { name: 'non-object-body', kind: 'invalid-params', description: 'a non-object body is structurally invalid', params: [], error: { code: 'INVALID_ARGUMENT', message: 'params must be a JSON object' } },
      { name: 'daemon-restoring', kind: 'domain-error', description: 'the daemon is restoring and cannot answer', params: { filter: null }, error: { code: 'DAEMON_NOT_READY', message: 'the daemon is restoring', data: { reason: 'restore' } } },
      { name: 'unknown-filter', kind: 'semantic-param-error', description: 'a schema-valid filter is rejected by the handler', params: { filter: 'unknown' }, error: { code: 'INVALID_ARGUMENT', message: 'unknown filter' } },
    ],
  );
  const write = operation(
    'demo.write',
    {
      type: 'object',
      properties: { actor: { type: 'string', minLength: 1 }, setupToken: { type: 'string', minLength: 1 } },
      required: ['actor', 'setupToken'],
      additionalProperties: false,
    },
    {
      type: 'object',
      properties: { ok: { const: true }, actor: { type: 'string', minLength: 1 }, removed: { type: 'array', items: { type: 'string' } } },
      required: ['ok', 'actor', 'removed'],
      additionalProperties: false,
    },
    [
      errorShape('INVALID_ARGUMENT'),
      errorShape('TARGET_NOT_AUTHORIZED', {
        type: 'object',
        properties: { actor: { type: 'string' } },
        required: ['actor'],
        additionalProperties: false,
      }),
    ],
    [
      { name: 'removed', kind: 'success', description: 'one synthetic file was removed', params: { actor: 'agent-a', setupToken: SENTINEL }, result: { ok: true, actor: 'agent-a', removed: ['provider.json'] } },
      { name: 'missing-actor', kind: 'invalid-params', description: 'an empty actor is rejected before dispatch', params: { actor: '', setupToken: SENTINEL }, error: { code: 'INVALID_ARGUMENT', message: 'actor must be a non-empty string' } },
      { name: 'foreign-target', kind: 'domain-error', description: 'the caller may not mutate another agent', params: { actor: 'agent-b', setupToken: SENTINEL }, error: { code: 'TARGET_NOT_AUTHORIZED', message: 'the target agent is not authorized', data: { actor: 'agent-b' } } },
    ],
    { empty: 'the write either returns its removal receipt or fails; it has no legal empty result' },
  );
  const get = operation(
    'demo.get',
    {
      type: 'object',
      properties: {
        id: { type: 'string', minLength: 1 },
        note: { type: 'string' },
        idempotencyKey: { type: 'string' },
      },
      required: ['id'],
      additionalProperties: false,
    },
    {
      type: 'object',
      properties: {
        ok: { const: true },
        id: { type: 'string' },
        idempotencyKey: { type: 'string' },
        entries: { type: 'array', items: { type: 'object', properties: { text: { type: 'string' } } } },
      },
      required: ['ok', 'id'],
    },
    [errorShape('INVALID_ARGUMENT')],
    [
      { name: 'first', kind: 'success', description: 'the first entry', params: { id: 'one', note: 'fixed', idempotencyKey: 'key-1' }, result: { ok: true, id: 'one', idempotencyKey: 'key-1', entries: [{ text: 'fixed' }] } },
      { name: 'second', kind: 'success', description: 'the second entry', params: { id: 'two' }, result: { ok: true, id: 'two', entries: [] } },
      { name: 'bad-id', kind: 'invalid-params', description: 'an empty id', params: { id: '' }, error: { code: 'INVALID_ARGUMENT', message: 'id must not be empty' } },
    ],
    { empty: 'an entry is always present', domainErrors: 'no declared domain error' },
  );
  const paths: Doc = {};
  for (const item of [list, write, get]) paths[`/daemon/request/${item.operationId}`] = { post: item };
  return {
    openapi: '3.1.0',
    info: { title: 'synthetic', version: '1' },
    paths,
    'x-hyprial-methods': [],
  };
}

export function syntheticTable(document: Doc = syntheticOpenapi()): ContractTable {
  return withDigest(projectContract(document, realEnvelope()));
}

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value));
}
