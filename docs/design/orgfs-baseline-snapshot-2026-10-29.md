# OrgFS baseline snapshot（0.5.5.2 协议扩展）

状态：设计草案，待 Allen 定稿；不改变 0.5.5.1 节点的现有行为。
关联：#2069、`docs/notes/orgfs-journal-compaction-2026-10-29/ANALYSIS.md`。

## 问题

0.5.5.1 的 `snapshotPoints` 只服务于 purge/replacement。`MeshSync._install_sync_snapshot`
收到 snapshot 时必须找到对应的 retirement record，否则返回 `snapshot-barrier`；因此
“保留最新 snapshot、删除旧 commits”无法让落后节点安装，直接删只会把老节点挡在同步外。

Allen 已接受删除历史造成的成员误拒（重新邀请即可），但要求同时扩展协议，避免未来
再次积累同类历史。目标是在不伪造 retirement、不让老节点静默丢数据的前提下，允许
**baseline snapshot** 作为老节点追赶的起点。

## 协议形状

`snapshotPoints[docId]` 增加可选字段：

```json
{
  "frontier": "<b64 state vector>",
  "snapshotId": "<snapshot id>",
  "planId": "journal-compaction-...",
  "baseline": true
}
```

- `baseline: true` 表示该 snapshot 是完整的文档物化状态，不是 purge 的 replacement。
- 只有带 `baseline: true` 的 point 才允许绕过 retirement record 校验；旧格式行为不变。
- `meta` 不产生 baseline point，也永远不删除 meta commits（否则成员/ACL 历史丢失）。
- 删除规则：只删除 `broadcast_at IS NOT NULL`、版本已被 baseline 覆盖、且属于该 doc 的行；
  `broadcast_at IS NULL` 的行一律保留。每个 doc 保留 snapshot 行本身和未广播尾部。

## 落点（0.5.5.2）

1. `LocalSpaceStore` 生成完整 snapshot（已有 `snapshot(..., shallow_since=None)`），
   compaction 工具写入 `baseline: true` 的 `snapshotPoints`。
2. `MeshSync._install_sync_snapshot`：当 point 带 `baseline` 时，校验 manifest 的
   `docId/frontier/snapshotId` 与 point 一致，直接用完整 update 安装；不要求 retirement。
   安装后只重放本地 state vector 未覆盖的本地 commits；如本地有分叉，保留双方并
   走现有 CRDT 合并，不覆盖未覆盖的本地更新。
3. 0.5.5.1 节点收到 baseline point 仍走原 `snapshot-barrier`，不会误装。Allen 已裁定
   **现在就删、不等全员 0.5.5.2**；`--apply` 以显式 `--accept-lagging-peers` 作为风险确认，
   接受离线老节点追不上、被误拒成员重新邀请。协议扩展仍随 0.5.5.2 交付，供新节点追赶。
4. `snapshot-barrier` 的错误详情增加 `baselineRequired`（仅诊断），不改变旧节点退出码。

## 回滚与失败

- `--apply` 先完整复制 `journal.sqlite3`、`-wal`、`-shm`、`meta.loro`、`tree.loro`、`docs/`
  到 `state/orgfs/compaction-backups/<spaceId>-<utc>/`（必须在 `spaces/` 之外）；
  任一步失败按备份原样恢复。
- 启动时 `spaces/` 下的非 UUID 目录（包括旧工具留在里面的 `.compact-backup-*`）
  以 `ORGFS_INVALID_SPACE_DIRECTORY` 失败并点名路径；不再被当成一个空间加载，
  也不再报“envelope belongs to another space”。
- `--apply` 结束前关闭写句柄，用 daemon 同款 `LocalSpaceStore` 重新打开压缩后的空间并
  比较每个 doc 的物化 hash；打不开或不一致即恢复备份。
- 只有 daemon stopped 才允许 apply（当前实现检查 `state/daemon.sock`）。
- 前后对每个 doc 的物化内容做 SHA-256 对比；不一致即回滚。
- 协议层不自动回滚已安装的 baseline；老节点升级后从 baseline 继续，若需回到旧历史，
  只能恢复备份文件（因此 apply 与备份必须在同一维护窗口）。

## 验证

- 合成 5 行 journal apply 后：行数下降、doc hash 不变、备份可恢复（已有测试）。
- 双节点：0.5.5.2 A 压缩 → 空 B 加入并同步 → B 的 doc state 与 A 一致；B 在压缩后新增
  的更新不丢。
- 老节点：0.5.5.1 B 收到 baseline → `snapshot-barrier`，且 B 的本地状态不变（红测锁旧行为）。
- mutation：删除 `baseline` 分支后双节点追赶测试必须失败；删除“未广播行保留”后测试失败。
- 真实副本：由 e2e-verifier 在 2.8GB journal 副本上跑 dry-run、apply、重启和 peer catch-up；
  记录前后大小、启动时间、峰值内存、hash 对比。

## 未决

- apply 的维护窗口与显式风险确认文案；不再要求“全员 0.5.5.2”作为前置门。
- baseline 安装后是否保留一个只读的 `compaction` retirement 记录用于审计；若保留，需要
  明确它不参与 snapshot 安装校验。
- 是否把 apply 收进 daemon CLI（当前是 `scripts/` 工具）；在协议定稿前不做。
