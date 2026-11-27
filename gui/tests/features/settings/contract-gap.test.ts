// @vitest-environment node
// Pin the approved HTTP surface, including Secret inventory and revocation.
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// B1/B2 gap evidence: the real HTTP-exposed daemon request surface.
const document = JSON.parse(await readFile(
  fileURLToPath(new URL('../../../../contract/daemon-request/openapi.json', import.meta.url)),
  'utf8',
)) as {
  paths: Record<string, Record<string, { operationId: string; 'x-hyprial-http-binding'?: unknown; 'x-hyprial-examples'?: unknown }>>;
  'x-hyprial-methods': Array<{ name: string; httpExposed?: boolean }>;
};

const operations = Object.entries(document.paths).flatMap(([path, entry]) =>
  Object.entries(entry).map(([method, operation]) => ({ path, method, ...operation })));
const ids = new Set(operations.map(operation => operation.operationId));
const declared = new Set(document['x-hyprial-methods'].map(method => method.name));
const httpExposed = new Set(document['x-hyprial-methods']
  .filter(method => method.httpExposed === true).map(method => method.name));

describe('daemon request HTTP projection gap', () => {
  // The count pin forces every new HTTP exposure through review: bump it only
  // together with the approved-method list below.
  it('pins the integrated HTTP-exposed method count', () => {
    expect(operations).toHaveLength(122);
    expect([...ids].sort()).toEqual([...httpExposed].sort());
    expect(ids.has('management.adapter.remove.preview')).toBe(true);
    expect(ids.has('management.adapter.remove')).toBe(true);
    expect(ids.has('agent.destroy.preview')).toBe(true);
    expect(ids.has('agent.destroy')).toBe(true);
    expect(ids.has('org.invites.list')).toBe(true);
    expect(ids.has('org.receivers.get')).toBe(false);
    expect(ids.size).toBe(operations.length);
    expect(operations.every(operation => operation.method === 'post')).toBe(true);
    expect(operations.every(operation => operation['x-hyprial-http-binding'] !== undefined)).toBe(true);
  });

  it('keeps hosts and top.snapshot HTTP-bound with declared examples', () => {
    for (const method of ['hosts', 'top.snapshot']) {
      const operation = operations.find(candidate => candidate.operationId === method);
      expect(operation, method).toBeTruthy();
      expect(operation?.path).toBe('/daemon/request/' + method);
      expect(operation?.['x-hyprial-http-binding']).toBeDefined();
      expect(operation?.['x-hyprial-examples']).toBeDefined();
    }
  });

  it('requires a nonempty examples array on every HTTP projection', () => {
    const methodsMissingExamples = operations
      .filter(operation => {
        const examples = operation['x-hyprial-examples'];
        return !Array.isArray(examples) || examples.length === 0;
      })
      .map(operation => operation.operationId);
    expect(methodsMissingExamples).toEqual([]);
  });

  it('exposes only the approved new adapter and message HTTP methods', () => {
    for (const method of [
      'adapter.list',
      'adapter.identities.list',
      'adapter.identities.sync',
      'adapter.register',
      'adapter.onboard.begin',
      'adapter.onboard.operation',
      'adapter.onboard.cancel',
      'adapter.routes.list',
      'adapter.routes.candidates',
      'adapter.routes.add',
      'adapter.routes.remove',
      'adapter.authorization.status',
      'adapter.authorization.begin',
      'adapter.authorization.operation',
      'adapter.authorization.cancel',
      'adapter.reload',
      'adapter.start',
      'adapter.status',
      'adapter.stop',
      'agent.cell.get',
      'agent.cell.update',
      'agent.rename',
      'message.history',
      'message.query',
      'message.send',
      'message.send.key_lookup',
      'message.send.lookup',
      'progress.list',
      'targets',
    ]) {
      expect(ids.has(method), method).toBe(true);
    }
    for (const method of ['message.pending.batch', 'message.status', 'message.conversations']) {
      expect(ids.has(method), method).toBe(false);
    }
  });

  it('exposes the cell-model conversation reads, keeping attach IPC-only and retiring link/threads', () => {
    for (const method of ['conversations.list', 'conversations.history']) {
      expect(ids.has(method), method).toBe(true);
    }
    for (const method of ['threads.get', 'threads.list', 'link.list', 'link.stats', 'link.add', 'link.drop']) {
      expect(ids.has(method), method).toBe(false);
    }
    // agent.attach/detach are IPC-only in 0.5.5.4 too (R6, Allen 2026-11-07).
    for (const method of ['agent.attach', 'agent.detach']) {
      expect(ids.has(method), method).toBe(false);
    }
  });

  it('does not invent settings methods outside the runtime contract', () => {
    for (const method of [
      'daemon.status',
      'autoupdate.status',
      'upgrade.steps.status',
      'log.page',
      'identity.users.list',
      'identity.users.add',
      'agent.secret.provider-write',
      'agent.secret.provider-list',
      'agent.secret.provider-configure',
      'agent.secret.provider-test',
      'agent.secret.grant',
      'agent.secret.revoke',
      'agent.provider.auth.list',
    ]) {
      expect(ids.has(method), method).toBe(true);
    }
    for (const method of [
      'daemon.shutdown',
      'logs.page',
      'user-proxy.inspect',
      'local-settings.set',
      'profile.list',
      'doctor.run',
      'agent.provider.auth.status',
    ]) {
      expect(ids.has(method), method).toBe(false);
    }
    expect(document['x-hyprial-methods']).toHaveLength(167);
    expect(declared.size).toBe(document['x-hyprial-methods'].length);
    expect(declared.has('org.personal.ensure')).toBe(false);
    expect(declared.has('agent.rename')).toBe(true);
    expect(declared.has('teleport.run')).toBe(true);
    for (const id of ids) expect(declared.has(id), id).toBe(true);
    for (const method of [
      'conversations.list', 'conversations.history',
      'conversations.create', 'conversations.get',
      'conversations.members.add', 'conversations.members.remove',
      'agent.attach', 'agent.detach',
    ]) {
      expect(declared.has(method), method).toBe(true);
    }
    for (const method of ['threads.get', 'threads.list', 'link.list', 'link.stats', 'link.add', 'link.drop']) {
      expect(declared.has(method), method).toBe(false);
    }
    expect(declared.has('message.pending.batch')).toBe(true);
    expect(declared.has('message.status')).toBe(true);
    expect(declared.has('message.conversations')).toBe(false);
  });
});
