/**
 * 测试专用：把旧的 fake request port 包成公共 GeneratedClient。
 *
 * 产品代码只接受真实注入的 GeneratedClient；这里仅让现有 fake-port 测试
 * 继续覆盖同一组生成方法，不进入产品入口。
 */
import { createGeneratedClient } from '../../src/client/generated';
import type { OrgBackendClient } from '../../src/features/org/impl/api/backend';
import { createOrgLiveSource } from '../../src/features/org/impl/domain/live-source';
import {
  createOrgRequestApi,
  type OrgRequestPort,
} from '../../src/features/org/impl/api/org-request-api';
import { createOrgfsRequestApi } from '../../src/features/org/impl/api/orgfs-request-api';
import type { OrgDomainSource } from '../../src/features/org/impl/domain/context';
import type { OrgRequestApi } from '../../src/features/org/impl/api/org-request-api';
import type { OrgfsRequestApi } from '../../src/features/org/impl/api/orgfs-request-api';

export function orgBackendFor(port: OrgRequestPort): OrgBackendClient {
  return createGeneratedClient({
    async invoke<TResult>(
      method: string,
      params: object = {},
    ): Promise<TResult> {
      return (await port.request(
        method,
        params as Record<string, unknown>,
      )) as TResult;
    },
  });
}

/** 兼容现有 fake-port 测试；产品入口不走这个 helper。 */
export function createOrgLiveSourceForPort(port: OrgRequestPort): OrgDomainSource {
  return createOrgLiveSource(orgBackendFor(port));
}

/** 兼容现有 fake-port 测试；生产直接注入 GeneratedClient。 */
export function createOrgRequestApiForPort(port: OrgRequestPort): OrgRequestApi {
  return createOrgRequestApi(orgBackendFor(port));
}

export function createOrgfsRequestApiForPort(
  port: OrgRequestPort,
): OrgfsRequestApi {
  return createOrgfsRequestApi(orgBackendFor(port));
}
