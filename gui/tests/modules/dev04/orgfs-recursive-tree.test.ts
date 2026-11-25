import { describe, expect, it } from 'vitest';
import { createOrgfsRequestApiForPort } from '../../support/org-backend';

function fakePort() {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  return {
    calls,
    async request(method: string, params: Record<string, unknown>) {
      calls.push({ method, params });
      return { nodes: [] };
    },
  };
}

describe('orgfs 嵌套文件树', () => {
  it('listNodes 始终请求 recursive，路径可选', async () => {
    const port = fakePort();
    const api = createOrgfsRequestApiForPort(port);

    await api.listNodes('space-1');
    await api.listNodes('space-1', 'dir');

    expect(port.calls).toEqual([
      { method: 'orgfs.ls', params: { spaceId: 'space-1', recursive: true } },
      {
        method: 'orgfs.ls',
        params: { spaceId: 'space-1', path: 'dir', recursive: true },
      },
    ]);
  });
});
