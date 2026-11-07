# org.create 可恢复提交（2026-10-19）

## 事故与目标

`org.create` 原先用多个独立 OrgFS commit 建目录 space、ACL space、ACL 文档、
ACL ID 绑定和 `directory/org.json`。任一步失败都不会回滚此前 commit。目录 space
一旦存在，重试会先走尚不可用的 ACL 策略检查，因此创建者也得到
`ORG_ACTION_NOT_AUTHORIZED`。本改动把创建改为创建者专属、可恢复的提交序列；不把
`create` 权限开放给其他用户。

## 输入文档核对

当前工作树和全部本地 Git refs 均没有 `ensure-group-design.md`，历史内容搜索也没有
`GROUP_PERM_FAILED`。因此本修复以现行 `org.create` 实现和线上实际返回的
`ORG_ACTION_NOT_AUTHORIZED` 为契约依据，没有臆造 `GROUP_PERM_FAILED` 映射。若该文档
位于外部仓库或未获取的 ref，后续必须用明确路径或 ref 单独核对。

## 端到端步骤和失败语义

1. CLI `hyprial org create NAME` 在
   `shell/impl/cli/commands/org/commands.py` 规范化为 `group-NAME`，发送
   `org.create`。默认 IPC round-trip 上限为 15 秒。超时返回 `IPC_TIMEOUT`，CLI
   不自动重发，也不能据此判断 daemon 是否已提交。
2. IPC server 完整读取一帧后同步执行 handler，再写响应。客户端在执行中断开不会
   取消 handler；handler 会继续到成功或异常，随后写响应时才看到 broken pipe。
3. `ipc_org_create` 构造 `OrgNetworkService`。若配置了 Casdoor，先确认 group 和
   owner；失败发生在任何 OrgFS 写之前。
4. service 在本机 `state/org/create-intents.json` 原子写入创建意图。记录绑定
   `org + nodeId + owner`，并逐步保存 directory/ACL space ID。文件损坏按无记录
   处理，不会据损坏内容授权。
5. 创建 directory space。`create_space` 自身的 genesis commit 失败会移除内存中的
   半成品；成功后 space 已 durable。随后记录其 ID，并立即写 `directory/org.json` 的
   `state=creating`。两步之间崩溃时，没有 lifecycle marker 的 unbound space 对外报告
   `partial`；重试用唯一同名 space、space owner 和创建意图恢复，不新建第二个 space。
6. 创建 ACL space并记录 ID。两步之间崩溃时，重试只复用本次 intent 开始后、同一
   owner、同名的唯一 ACL candidate；歧义时返回 `ambiguous-space`，不猜测。
7. 幂等写默认 `policy/role-grants.json`，把实际 `aclSpaceId` 写入 directory
   space 的 owner-only meta，并创建共享目录 roots。任一步失败都保留 intent；同一
   创建者重试从已有 ID 继续。
8. 删除 intent 后尝试发布设备，最后才把 lifecycle 更新为 `state=active`。发布失败返回
   `published:false`（兼容字段 `devicePublished:false`），CLI 以 `ok:false` 和退出 1
   报告，但不撤销已完成的组织；同名重试只重试缺失步骤，不重复创建 space。

已有的 pre-fix orphan 没有 intent。唯一同名 directory space 的 owner 等于当前
owner 且 ACL 绑定缺失/失效时，允许该 owner 建立一条 recovery intent 并恢复；其他
owner 始终得到 `ORG_ACTION_NOT_AUTHORIZED`。有效 ACL 绑定存在时，重复 create 仍走
正常 ACL policy，不能借恢复路径绕过策略。

## 运维面

`hyprial org repair [NAME]` 默认 dry-run，经 `org.repair` 返回：

- `apply`: 是否实际修复；默认 `false`；
- `candidates`: `{org, spaceId, reason, repairable}`；
- `repaired`: 仅 `--apply` 后成功完成的标准 `org.create` 结果。

只列 ACL 绑定缺失、未知或 owner 不匹配，或已绑定但缺默认 grant / directory
生命周期文档的唯一 directory space。只有当前 owner，且已有 intent 也属于当前
node 的 space 标成 `repairable`；`--apply` 复用同一 creator-only 恢复路径。命令不会删除
space，避免在没有 OrgFS space-delete 原语和复制确认时制造更大的数据丢失。不得把
repair 对生产运行当作测试。
