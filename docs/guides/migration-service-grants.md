# 迁移说明：组织目录新增受保护段 `service-grants`

> [!IMPORTANT]
> 这是同一版本四项改动之一。**升级顺序以 [升级说明：把已有 org 升级到服务授权版本](migration-service-grants-upgrade.md) 为准**；本文只解释授权文档目录这一项。

适用于：把一个 org 内的 hyprial 设备升级到支持服务授权文档（`service.grant`、`service.revoke`、`service.grants`）版本的管理员，或代为操作的 agent。

一句话：**没有迁移代码，也没有专用迁移命令**。对已有 org，**org owner 的 daemon 升级后运行一次即可**：它在发布本机设备行时，为本机是 space owner 的每个 org 补建缺失的 `directory/service-grants` 父目录，成员随后才能写下第一份授权；**同一 org 内的所有设备应在创建第一份授权前完成升级**（见第 2、3 节）。之后各成员的授权与撤销直接写进自己名下的路径，不再经过 org owner。

> [!NOTE]
> 本文明确区分**实测**（在真实 OrgFS 存储与单元测试中运行验证过的行为）与**读代码推断**（由源码逻辑推导、未做跨版本联机实测的行为）。

## 1. 这次改动做了什么

- 在 org 目录空间新增受保护段 `service-grants`，路径为 `directory/service-grants/<owner>/<grantId>.json`（`grantId` 格式为 `sg.` 加 26 位小写 base32 `[a-z2-7]`）。文档格式见 [contract/service-grants](../../contract/service-grants/README.md)。
- 作者规则与 `devices` 相同：仅路径上的 `<owner>` 可以创建或修改自己名下的子目录与授权文档（**实测**：已在真实 `OrgFsRuntime` 上验证非该 owner 的成员写/覆写或创建他人路径均被 OrgFS 拒绝）。这条规则假定更新的作者是真实的，作者没有签名（见 [已知问题](../reference/known-problems.md) KP-139）。
- `directory/service-grants` 已加入 owner 创建的目录表 `_OWNER_CREATED_DIRS`。
- 新增三个本地操作员 IPC 方法与 CLI 子命令：`hyprial org service grant|revoke|grants`（`audience=("local-operator",)`，`http_exposed=False`）。`--org` 写完整的目录空间名 `group-<name>`。
- 对同一端口、同一接收方再次授权时，若已有有效授权则原地更新（授权 id 不变）。本人名下已撤销或已过期超过 7 天的文档，在本人下一次授权或撤销时自动删除；每个授权人在每个 org 名下最多 256 份文档，清理后仍达 240 份时拒绝新发授权 id。

## 2. 已有 org 何时创建 `directory/service-grants`

- **新建 org**：org owner 执行 `hyprial org create <org>` 时创建，无需额外步骤。
- **已有 org**：升级后的 org owner daemon 在发布本机设备行时（**设备行发布周期**：sidecar 写出地址文件后立即一次，之后至多每 10 秒一次），对本机 owner 是 space owner 的每个 org 补建缺失的 owner 目录（实现见 `src/hyprial/daemon/impl/org/network/grant_roots/`）。
  - 每个 org 在 daemon 进程内成功一次后不再检查；失败只记一条 `org.directory_roots.failed` 警告（组件 `org`，只含 org 与错误类型），下一次发布时重试，**不影响设备行发布**（**实测**：`tests/daemon/service_grants/fixes/test_owner_roots_backfill.py` 覆盖补建、只建一次、失败重试且设备行照常发布、他人拥有的 org 不补建）。
  - 非 owner 成员无权创建两级目录 `directory/service-grants`（其受保护树节点归属 `space_owner`）；目录未建时成员的 `service.grant` 返回 `SERVICE_UNAVAILABLE`，文案为 "org service-grants directory is not initialized yet; it is created the next time the org owner's upgraded daemon publishes its device row"。
- 备选：org owner 也可以对已有 org 重跑一次幂等的 `hyprial org create <org>`（返回 `existed: true`，不改动既有 org 元数据），效果相同（**实测**：同上测试文件）。
- 一旦 `directory/service-grants` 建好并同步到各成员副本，成员即可自行在 `directory/service-grants/<owner>/<grantId>.json` 下创建和撤销授权，之后不再依赖 org owner 在线或转抄。

所以暴露方不是 org owner 时，**org owner 必须先在线**，见总说明第 1 节。

## 3. 为什么同一 org 内设备需在首次授权前完成升级（读代码推断）

以下结论基于对 `src/hyprial/identity/impl/org_directory/models.py` 与 `src/hyprial/daemon/impl/orgfs/storage/store/admission.py` 的阅读推导（**未做新旧版本双进程跨机联机实测**）：

1. **owner 仅创建父目录 `directory/service-grants`**：两级路径在旧版的 `_protected_tree_principal` 中同样回落到 `space_owner`，且作者就是 `space_owner`，旧版副本可以接纳该树节点更新。
2. **任一成员（含 org owner）写出第一份授权文档**：
   - 文档 ID `protected_directory_doc_id` 编码了段名 `"service-grants"`。旧版副本的 `parse_protected_directory_doc_id` 检查 `section in _PROTECTED_DOC_SECTIONS` 时不认识 `"service-grants"`，抛出 `ValueError`，准入层返回拒绝（`not-a-member`）。**每一份授权文档更新都会被旧版副本拒绝。**
   - 若授权人是非 owner 成员（例如 `bob`），其在 `tree-<spaceId>` 中写入的 `directory/service-grants/bob` 节点 ID 编码的主体是 `user:bob`；旧版副本的 `_protected_tree_principal` 不包含 `"service-grants"`，会回落到 `space_owner`，计算出的期望节点 ID 与报文中不一致，导致**该成员的树更新也被旧版副本拒绝**（在旧版副本上可能阻滞该 writer 后续 `tree-<spaceId>` 更新的连续序号推进，直到副本升级）。

因此：虽然仅升级 owner 并由其 daemon 建父目录本身不破坏旧副本，但**在 org 内发出第一份 `service.grant` 之前，应先把同一 org 内所有成员设备升级到本版本**。设备加密公钥（[迁移说明：目录设备行携带设备加密钥公钥](migration-device-encryption-key.md)）与 sidecar 协议 v7（[迁移说明：hyprial-tailcat stdio 协议升级到 v7](migration-sidecar-protocol-v7.md)）属于同一版本，在同一个维护窗口一并完成。

## 4. 确认父目录已建

完整的停机、安装、启动顺序见总说明第 3 节。org owner 启动并完成一次设备行发布后，在任意已启动的设备上验证父目录已建（这一步能区分"已建 / 未建"；只读 `service.grants` 在目录缺失时同样返回空列表，不能用来判断）。先找到该 org 的目录空间 ID，再列出 `directory`：

```sh
hyprial org fs space list --json
hyprial org fs ls <spaceId> directory --json
```

已建：列表中有名为 `service-grants`、类型为目录的条目。未建：没有该条目；此时确认 org owner 的 daemon 已升级并在运行，查看其日志中是否有 `org.directory_roots.failed`，或由 org owner 重跑一次 `hyprial org create <org> --json`（预期 `"existed": true`）。

## 5. 回退与范围说明

- 若在已有授权文档写入之后把某台设备回退到不支持服务授权的版本，该旧版副本会拒绝同步 `service-grants` 文档以及非 owner 成员的 `directory/service-grants/<owner>` 树更新（见第 3 节，读代码推断），直到该设备重新升级。回退必须整个 org 一起做，见总说明第 5 节。
- 暴露方怎样按授权放行见 [迁移说明：暴露端口默认拒绝](migration-service-exposure-default-deny.md)；接收方看到什么、还要手工做什么见 [服务授权：接收方指南](service-grants-recipient.md)。
