# OC4 取消个人 org、本机凭据授权与遗留 binding.json 清理迁移指南

> 自 OC4 起，产品代码不包含任何自动迁移、自动改写或兼容双写逻辑；如旧 home 仍残留 `personal-*` 个人组织 space、OrgFS 凭据授权或遗留 `binding.json` 目录行，需由维护 agent 或操作员按本指南手工核对处理。

## 1. 变更摘要

- **取消个人 org（J10）**：移除 `ORG_KIND_PERSONAL`、`personal_org_for`、`ensure_personal_org`、`resolve_personal_org`、`org.personal.ensure`、`AGENT_LAST_ORG` 与 `AGENT_PERSONAL_ORG`。仅保留工作组织（`group-*`）；agent 所属组织列表允许为空（可离开最后一个工作组织）。
- **本机归属与模型凭据授权存储（J22）**：`org/membership/owner_store.py` 改为 `<HYPRIAL_HOME>/state/owner-store/` 本机存储；模型凭据记录与授权（`provider_keys`）默认存于本机 `<HYPRIAL_HOME>/state/provider-keys/store/`，不再依赖个人 org 或跨设备同步。
- **移除遗留 `binding.json` 读写面（J21）**：`directory/people/<slug>/binding.json` 不再存在任何产品代码读写面；**注意保留 `<HYPRIAL_HOME>/state/tailcat/machine-binding.json`（Tailcat 机器绑定）勿删**。

## 2. 迁移前备份

在修改旧 home 前先备份状态目录（请替换为实际的 `HYPRIAL_HOME` 路径）：

```sh
HYPRIAL_HOME="${HYPRIAL_HOME:-$HOME/.hyprial}"
cp -a "$HYPRIAL_HOME/state" "$HYPRIAL_HOME/state.bak-pre-oc4"
```

## 3. 人工核对与迁移步骤

### 3.1 检查残留的 `personal-*` space

1. 使用 `hyprial org list --json` 检查当前可见组织；新版本只承认 `group-*` 工作组织。
2. 如需导出旧 `personal-*` space 中的普通笔记文档，请先重启前用 `hyprial orgfs ls` / `hyprial orgfs read` 或 `orgfs.export` 导出所需文件。
3. 确认已导出或无需保留后，可在离线备份完成后清理对应只读遗留 space，或使用工作组织（`hyprial org create`）承载协作目录。

### 3.2 重建本机模型凭据与 agent 授权

升级前曾写入 OrgFS 个人空间的 provider key / agent grant 不会自动搬迁到本机：

1. 确认 `<HYPRIAL_HOME>/settings.json` 中的 `providerKeys.authority` 与所需模型配置有效。
2. 通过 `hyprial agent secret` CLI 或 GUI Settings → Provider Keys 重新写入本机 provider secret 与 agent 授权（写入路径为 `<HYPRIAL_HOME>/state/provider-keys/store/`）。
3. 使用 `hyprial daemon restart` 并检查日志及 CLI 输出确认已恢复。

### 3.3 确认遗留 `binding.json` 与 Tailcat `machine-binding.json`

1. 工作组织内 `directory/people/<userKey>/binding.json` 若仍存在，可归档备查；新版本不再读取或生成该文件。
2. 切勿删除 `<HYPRIAL_HOME>/state/tailcat/machine-binding.json`，它是本机 Tailcat 设备身份绑定，仍由 `identity.impl.device.machine_binding` 正常使用。
