# 迁移说明：组织目录新增受保护段 `service-grants`

适用于：把一个 org 内的 hyprial 设备升级到支持服务授权文档（`service.grant`、`service.revoke`、`service.grants`）版本的管理员，或代为操作的 agent。

一句话：**没有迁移代码，也没有专用迁移命令**。对已有 org，org owner 升级后需要在 owner 的设备上为每个已有 org 执行一次 `hyprial org create <org>`（幂等，用于补齐 `directory/service-grants` 父目录），成员才能写下第一份授权；**同一 org 内的所有设备应在创建第一份授权前完成升级**（见第 2、3 节）。之后各成员的授权与撤销直接写进自己名下的路径，不再经过 org owner。

> [!NOTE]
> 本文明确区分**实测**（在真实 OrgFS 存储与单元测试中运行验证过的行为）与**读代码推断**（由源码逻辑推导、未做跨版本联机实测的行为）。

## 1. 这次改动做了什么

- 在 org 目录空间新增受保护段 `service-grants`，路径为 `directory/service-grants/<owner>/<grantId>.json`（`grantId` 格式为 `sg.` 加 26 位小写 base32 `[a-z2-7]`）。
- 作者规则与 `devices` 相同：仅路径上的 `<owner>` 可以创建或修改自己名下的子目录与授权文档（**实测**：已在真实 `OrgFsRuntime` 上验证非该 owner 的成员写/覆写或创建他人路径均被 OrgFS 拒绝）。
- `directory/service-grants` 已加入 owner 创建的目录表 `_OWNER_CREATED_DIRS`（`src/hyprial/daemon/impl/org/network/directory.py`）。
- 新增三个本地操作员 IPC 方法与 CLI 子命令：`hyprial org service grant|revoke|grants`（`audience=("local-operator",)`，`http_exposed=False`）。

## 2. 已有 org 何时创建 `directory/service-grants`（`ensure_org_directory_roots` 调用时机）

- **读代码核实**：`OrgFsDirectoryStore.ensure_org_directory_roots(org)` 在当前代码库中**仅有一处调用点**——`OrgNetworkService.create(org)`（`src/hyprial/daemon/impl/org/network/service.py:296`）。
  - `OrgNetworkService.create(org)` 仅由 `org.create`（即 `hyprial org create <org>`）以及面向未完成创建 org 的 `org.repair --apply` 触发。
  - **daemon 启动、恢复或后台维护循环都不会自动调用 `ensure_org_directory_roots`。**
- 因此：
  - **新建 org**：org owner 执行 `hyprial org create <org>` 时自动创建 `directory/service-grants`，无需额外步骤。
  - **已有活跃 org**：仅升级并重启 org owner 的 daemon **不会**自动创建 `directory/service-grants`；非 owner 成员在父目录尚不存在时执行 `service.grant` 会因缺少父目录而失败（`SERVICE_UNAVAILABLE`），且非 owner 成员无权自行创建两级目录 `directory/service-grants`（其受保护树节点归属 `space_owner`）。
  - org owner 升级后在自己设备上对每个已有活跃 org 执行一次 `hyprial org create <org>`：对已存在的活跃 org 该操作幂等（`existed: true`，保留既有目录空间与 ACL 空间），并执行 `ensure_org_directory_roots(org)` 补建缺失的 `directory/service-grants`（**实测**：`ensure_org_directory_roots` 创建 `directory/service-grants` 以及随后成员在 `directory/service-grants/<owner>/` 下读写已在真实 OrgFS 上验证；`org.create` 的幂等行为见既有测试 `test_create_is_idempotent_for_an_existing_space` 与读代码核实）。
- 一旦 `directory/service-grants` 建好并同步到各成员副本，成员即可自行在 `directory/service-grants/<owner>/<grantId>.json` 下创建和撤销授权，之后不再依赖 org owner 在线或转抄。

## 3. 为什么同一 org 内设备需在首次授权前完成升级（读代码推断）

以下结论基于对 `src/hyprial/identity/impl/org_directory/models.py` 与 `src/hyprial/daemon/impl/orgfs/storage/store/admission.py` 的阅读推导（**未做新旧版本双进程跨机联机实测**）：

1. **owner 仅创建父目录 `directory/service-grants`**：两级路径在旧版的 `_protected_tree_principal` 中同样回落到 `space_owner`，且作者就是 `space_owner`，旧版副本可以接纳该树节点更新。
2. **任一成员（含 org owner）写出第一份授权文档**：
   - 文档 ID `protected_directory_doc_id` 编码了段名 `"service-grants"`。旧版副本的 `parse_protected_directory_doc_id` 检查 `section in _PROTECTED_DOC_SECTIONS` 时不认识 `"service-grants"`，抛出 `ValueError`，准入层（`admission.py:405-407`）返回拒绝（`not-a-member`）。**每一份授权文档更新都会被旧版副本拒绝。**
   - 若授权人是非 owner 成员（例如 `bob`），其在 `tree-<spaceId>` 中写入的 `directory/service-grants/bob` 节点 ID 编码的主体是 `user:bob`；旧版副本的 `_protected_tree_principal` 不包含 `"service-grants"`，会回落到 `space_owner`，计算出的期望节点 ID 与报文中不一致（`admission.py:302`），导致**该成员的树更新也被旧版副本拒绝**（在旧版副本上可能阻滞该 writer 后续 `tree-<spaceId>` 更新的连续序号推进，直到副本升级）。

因此：虽然仅升级 owner 并执行 `hyprial org create <org>` 建父目录本身不破坏旧副本，但**在 org 内发出第一份 `service.grant` 之前，应先把同一 org 内所有成员设备升级到包含 S3 的版本**（与 S1 设备加密公钥升级窗口一致，建议同一维护窗口一并完成）。

## 4. 升级步骤

1. 确认 org 内所有设备可在同一维护窗口内升级（如正在同步升级 S1 设备加密公钥与 S4 sidecar v7，可合并在同一维护窗口）。
2. 在每台设备上停止 daemon 并安装新版本，再启动 daemon：

   ```sh
   hyprial daemon stop
   hyprial daemon status
   # 安装新版本（wheel 或源码）
   hyprial daemon start
   ```

3. 在 **org owner** 的设备上，对每个已有 org 执行一次幂等的 `org create` 以创建 `directory/service-grants` 父目录（把 `<org>` 替换为实际组织名，例如 `group-acme`）：

   ```sh
   hyprial org create <org> --json
   ```

   预期返回 `"ok": true` 且 `"existed": true`。

4. 等待 org 目录空间同步后，在任意成员设备上验证只读查询可用：

   ```sh
   hyprial org service grants --org <org> --json
   ```

   预期返回 `"ok": true`、`"granted": []`、`"received": []`。

## 5. 回退与范围说明

- 若在已有授权文档写入之后把某台设备回退到 S3 之前的版本，该旧版副本会拒绝同步 `service-grants` 文档以及非 owner 成员的 `directory/service-grants/<owner>` 树更新（见第 3 节，读代码推断），直到该设备重新升级。
- 本切片（S3）只落地授权文档存储、`service.grant|revoke|grants` 与只读查询；暴露方按授权集合放行（S4）以及接收方自动生成服务映射与落盘凭据（S5）由后续切片启用。
