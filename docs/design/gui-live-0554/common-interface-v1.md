# 公共接口 v1：四线并行开发入口

实现入口：`gui/src/app/backend/index.ts`。本包只交付内部注入接口和连接代际保护，
不启动宿主、不修改普通 App、不宣称 live/首次登录/wheel 已可用。运行时装配另包交付。

## 各线使用方式

```ts
import type { GeneratedClient } from '../../../client/generated';

type OrgBackendClient = Pick<GeneratedClient, 'org' | 'orgfs' | 'link' | 'hosts' | 'top'>;
// 由公共 capabilities 装配传入同一个 connection.client。
export function createOrgLiveSource(client: OrgBackendClient) { /* 本线 adapter */ }
```

`GeneratedClient` 仍由 `gui/scripts/generate-reference-client.mjs` 从 OpenAPI 生成，
公共入口也重导出该类型；方法、参数、结果不能另抄一套。agent、settings、chat 同样
用 `Pick<GeneratedClient, ...>` 取本线实际存在的 namespace/method。新增契约未生成前
明确缺口，不能用 `any`/通用字符串 invoke 假装该方法存在。

| 文件 | 责任与稳定面 |
|---|---|
| `gui/src/app/backend/types.ts` | `BackendDescriptor`、`BackendState`、`BackendConnection`；内部接口版本 1 |
| `gui/src/app/backend/connection.ts` | 唯一公共 generated-client 绑定工厂；一个 transport generation 对应一个实例；提供生命周期 owner 专用 `retire()` |
| `gui/src/app/backend/context.tsx` | `BackendProvider` / `useBackend`；无 provider 默认为 unavailable，不创建网络层或 fixture |
| `gui/src/client/generated/{index,types}.ts` | 业务方法/参数/结果的唯一生成类型，本包不修改生成物 |
| `gui/src/client/{transport,errors,types}.ts` | 唯一 HTTP 实现与 `ClientError`、dispatch、diagnostics；本包沿用其一次尝试语义 |
| `gui/src/app/capabilities.tsx` | 后续公共装配统一调用四线工厂；本包未改 |

当前 `createBackendConnection(transport, descriptor)` 的 transport 端口只供公共装配与测试。
后续由同一 `connection.ts` 的宿主会话加载器创建真正 `HttpTransport`，并绑定本工厂；
feature 不创建 transport/client、不读 URL/token/principal、不调用工厂。不允许把目前
直接 bearer 配置或测试替身接入产品浏览器，不能以空 token/same-origin-reference 代替会话认证。
运行时宿主的握手格式、同源认证与 HttpTransport 的扩展不是本接口版本的既成实现。

## 状态、换代、错误和回执

- `driver` 只有 `daemon | contract-mock`，是宿主已选模式的只读描述。工厂不据此选择
  endpoint、分支调用、读 env/query 或装 fixture；两个值使用完全相同的生成调用。
- `BackendState.status` 为 connecting / connected / transitioning / unavailable。
  connected 表示注入了连接，不表示所有业务可用。descriptor.phase 为 bootstrap /
  restoring / ready，仅展示宿主报告；真正阶段/权限/可调用性仍由 HTTP/daemon 契约裁决。
- `generation` 与 `catalogDigest` 必填，descriptor 拷贝后冻结；它们不是凭据。
  公共生命周期 owner 在切换/退出前同步 `retire()` 旧连接，再发布 transitioning 状态、
  完成新宿主握手并创建新连接。Provider 只注入，不私自停止连接或用 effect 延迟撤销。
- 被保留的旧 adapter 发起新调用时本地拒绝 `BACKEND_CONNECTION_RETIRED`，
  `dispatch=not-dispatched`、attempts=0；该码是客户端错误，不是新增 MethodError/HTTP 错误码。
  真实 HTTP/IPC 对旧 session/generation 的拒绝仍须独立实现，不能拿前端 guard 代替。
- 已发出的请求继续保留原始结果/错误；不重试、不转换 unknown、不宣称回滚。
  adapter 保留 `ClientError.code/dispatch/diagnostics.requestId` 和原始 domain data。
  业务 receipt/operationKey 取各方法生成结果；这里不另造通用业务回执 schema，
  onboarding 尚待契约包固定，缺字段不得自造成功。
- 绑定时固定 catalog digest；单次调用不能用 options 改 pin。generation/catalog 变化
  需要重建连接与 adapter，不在原实例上改值。服务端 digest 漂移仍由 HttpTransport 报错。

## 接入顺序与交付

1. 四线可立即按现有 GeneratedClient 编写窄 adapter；替身仅进测试。
2. 本地完成后先将固定 SHA 提交非作者 `cc-daemon-request-review`；REWORK 原作者修复，
   新 SHA 重新审核。未通过前不交 emergent 合并。
3. reviewer 通过后才推送/提交 PR 与审核证据给 emergent；由其合入 `intg/gui-live-0554`，广播 agent，并在飞书群通知
   **接口 ready**、合入 SHA、路径和上述未覆盖项；不得写成完整 GUI/运行时 ready。
4. 后续 M1 包补同源宿主/会话/driver/公共装配，接收各线已审导出，普通 App 进入统一链路。
   M2 补完整首次旅程；阶段放行见 `integration-gates.md`，冷启动设计义务仍保留。
