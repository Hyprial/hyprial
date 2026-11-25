/**
 * org 线对公共 GeneratedClient 的最小依赖面。
 *
 * 公共连接层只创建一个 GeneratedClient；组织适配器接受这里的 Pick 类型，
 * 不自行创建 transport，也不读取 URL、token 或 principal。
 */
import type { GeneratedClient } from '../../../../client/generated';

/** 组织生命周期读取/写入使用的方法组。 */
export type OrgRequestClient = Pick<GeneratedClient, 'org' | 'hosts' | 'top'>;

/** OrgFS 文件动作使用的方法组。 */
export type OrgfsRequestClient = Pick<GeneratedClient, 'orgfs'>;

/** live source 的完整注入面：一个 client 同时提供组织与文件。 */
export type OrgBackendClient = Pick<
  GeneratedClient,
  'org' | 'orgfs' | 'hosts' | 'top' | 'targets'
>;
