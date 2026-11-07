# daemon 恢复 runbook

以 2026-10-15 HQ 分区事故（转发面全天慢性抖动 → 夜间塌缩至单端点，多节点被波及）的
一手教训定型。目标读者：持有节点运维面的 agent / 值守者。受影响端完整时间线与读数见
[docs/notes/daemon-partition-autodl-firsthand-2026-10-15.md](../notes/daemon-partition-autodl-firsthand-2026-10-15.md)。

## 0. 先判读，再动手

区分三种局面，处置路径完全不同：

| 读数 | 判定 | 去向 |
|---|---|---|
| `hyprial doctor` 全绿，`hyprial host list` 骨干逐节点消失，消息 `queued/target-not-visible` | 转发面/对端分区，**本机 daemon 未死** | §5 分区纪律 + 找对端运维面 |
| doctor `daemon IPC` 失败 / daemon 进程不在 | 本机 daemon 死 | §1 正规启动 |
| daemon 在但 tailcat 子进程缺失 / hosts 恒空 | 安装缺件（平台二进制丢失） | §1 陷阱 B |

关键对照：受影响端 doctor 8/8 ok + 对端自测服务正常（HQ 侧 `forgejo.example-tailnet.ts.net:3000` HTTP 200）
而本机到该端点 3000/22/2222/443 全不通 ⇒ 判定为"这台机到该节点的可达性"问题，不是服务端故障。

## 1. 正规启动与两个陷阱

正规路径只有一条：**`hyprial init`**（自带守护监督与平滑重启：停旧代、验证退出、启换代、验证在线）。

- **陷阱 A（daemon run + nohup）**：`hyprial daemon run` 挂在工具 shell 下再 nohup，
  会被进程组清理连带杀死（本事故夜间 HQ daemon 两次重启期间的多代漂移与此同类）。
  工具 shell 里的任何启动方式都不得作为常驻手段；发现即按 §1 正规 init 重启。
- **陷阱 B（editable 源码树安装丢平台二进制）**：editable/源码树安装会丢
  `_bin/hyprial-tailcat` 平台二进制 → daemon 起得来、sidecar 缺件 → **跨机孤立**
  （本机现象即"daemon 健康但 mesh 不通"）。修复：从 uv 缓存或正式 wheel 补齐，
  然后 `hyprial init` 重启；验证 `hyprial doctor` 的 `tailcat-sidecar` 项为
  `sidecar verified`。

启动后验收：doctor 全绿 + `hyprial host list` 出现骨干 peer + 一条实测消息 fetched。

## 2. orphan 注册表膨胀：识别与过渡修剪

离线/漂移 agent 会在注册表留下 **uuid4 键族** orphan 条目（本事故 HQ 侧为主要增长源之一）。

识别（三条读数一起看）：

- 条目总量随 daemon 代数单调增长（重启不清零）；
- **无 pid 占比**升高（条目存在但对应进程早已不存在）；
- **按 agent 聚集**：同一 agent 名下多条不同 uuid（代际漂移残留）。

过渡修剪（在正式修复落地前）：

1. **备份**注册表（整体复制，保 0600）；
2. **修剪**已判定 orphan 的条目（无 pid 且**距最后 seen ≥ 24h，或跨 ≥ 3 个 daemon epoch**
   ——与 restore 缺省 12h 阈值同量级取整，避免"当日"在代际漂移下误判刚起的新代）；
3. `hyprial init` 重启后核对条目数与 hosts。

不可跳序：不备份直接修剪，一旦误删存活 agent 条目将放大故障。

## 3. 恢复后第一动作：排查悬置破坏性回合

**daemon 恢复会复活未完成的处置动作**——重启前悬置的 down / rm / close 一类破坏性回合，
可能在恢复后自动续跑。恢复完成后、放行常规作业前，依次：

1. 看 `hyprial daemon restart` 输出中的 interrupted worker turns 报告；
2. `hyprial workflow status` 逐图核对 in-flight：确认没有应当作废的旧请求被复活续跑；
3. `hyprial routine list` 核对 IN-FLIGHT 列：routine 的在途任务是否与实际相符；
4. `hyprial message outbox list` + `hyprial message status`：分清"该补投"与"该作废"
   （已 TTL 过期的旧请求不要盲目重发原 ID，按 §5 重发新副本）。

## 4. 离线 agent restore 重试 = 注册表增长引擎

daemon 重启后按 restore 策略复活离线 agent；**重试风暴本身会持续制造注册表条目**
（§2 的增长引擎之一）。止血：把问题 agent 的策略改为 `never`
（`state/agent-restore-policy.json`，per-agent 值 `active / always / never`，缺省 12h 阈值；
详见 [agent-restore-policy](../reference/agent-restore-policy.md)）。
止血只是止血：根因（为何离线、为何反复重试）仍要归档立案；
**根因闭合后把策略改回 `active`/`always` 并在事故单销项**——永久禁 restore 被遗忘
本身就是下一类故障（见上文 restore 文档的 dormancy 语义）。

## 5. 长分区下的分区纪律（受影响端实践）

- **默认 TTL ≈ 30 分钟**：`accepted+queued` 不等于必达。关键报告不要依赖单次入队，
  用**可见性触发重发 + `fetched` 终态确认**的状态机兜底：轮询 `hyprial host list`，
  目标可见且上一副本非 `pending` 即发新副本，`fetched` 后停。
- `hyprial message status` 四态（`pending / fetched / expired / unknown`）是唯一可信送达判据；
  "发出去过"不算数。
- **直连用户通道（Lark adapter，公网）不受 mesh 分区影响**：分区期间用于向 owner 同步与请求人工介入。
- **时钟疑报纪律**：shell 日期与文件 mtime / daemon epoch 相差"整天级"时，先排除
  daemon 管理的 worker session 跨天空闲（真实流逝），再用 ≥3 次程序化解析对照权威 HTTP Date
  后才能上报"时钟跳变"；单次粗解析（如 `date -d` 对畸形头部）会产出小时级假读数。
- 分区中的节点会**闪断闪回**（本事故 devmini / usrb-dev 反复进出 hosts）：
  单次 `hosts` 读数不下结论，以连续窗口为准。

## 6. 恢复完成判据（验收清单）

- [ ] doctor 全绿（含 `tailcat-sidecar verified`）
- [ ] `hyprial host list` 骨干全员在线且稳定 ≥ 2 个连续读数窗口
- [ ] 关键消息 `delivery status = fetched`（逐条核对，不以发送动作为准）
- [ ] workflow in-flight / routine IN-FLIGHT 与实际作业一致，无复活悬置回合
- [ ] 注册表条目数恢复增长前的合理水位，无持续 orphan 增长
- [ ] 事故时间线与读数落档（notes 证据文件），根因立案

## 参考

- 事故定型卡：参谋部 dispatch-coordinator 入门卡（2026-10-16），四条教训的权威摘要。
- 受影响端一手记录：[docs/notes/daemon-partition-autodl-firsthand-2026-10-15.md](../notes/daemon-partition-autodl-firsthand-2026-10-15.md)。
- HQ 侧台账：`HyprialOS/hq` `notes/current-state-hq-adjutant.md` @ `a36a9dfe`（已落 main，含 §892–§954；
  §909/§910 = init 正规路径与 nohup 陷阱，§912 = 悬置破坏性回合复活实证）。
- [agent-restore-policy](../reference/agent-restore-policy.md)：restore 策略字段与止血口径。
