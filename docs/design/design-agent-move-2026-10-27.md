# 设计：agent move —— 命令面统一、zenoh 传输、邀请/确认/自报

- 日期：2026-10-27
- 状态：**决策已定，实现未开始**。本文是 `intg/move-cmd-complete` 的设计基线；交付任务与验收口径在
  `docs/notes/handoff-agent-move-devusr-2026-10-27.md`。
- 决策人：Allen（2026-10-27 两条原话见 §0）。实现归属：devusr。
- 基线：`dev @ 3252c112c`（本文所有读数取自该树）。

---

## 0. 两条裁定（原文）

```
① "transfer 命令全部改为 agent move（或者只是 move），不要再保留 transfer（评论，文档也记得清理）"
② "agent move 不应该依赖 ssh 权限，应该走 hyprial 的 zenoh 传输，从而和未来的 zenoh acl 集成"
```

### 0.1 追加裁定与澄清（2026-10-27 第二轮讨论）

```
③ 不要 ssh 的**核心原因**：可迁移的 agent 不应该被放大到"获取 ssh 权限"。
   ⇒ 这是**权限放大**问题（一个可搬的 agent 不该顺带拥有目标机的 shell），
     不是"ssh 太慢/太土"。§3.1 的授权粒度分析是这个原因的下游结论，不是替代它。
④ 跨 org 的 move **不允许**（原 §7 Q1 关闭）。
⑤ 数据面候选：tailcat（见 §3.5）。控制面仍走 zenoh，字节是否走 tailcat 待实测。
```

### 0.2 第三轮：两处**更正**（我上一版说错了）

```
⑥ 「zenoh 没有流式原语」——**错**。我只读了仓内我们自己的封装（`transport/zenoh/transport.py`），
   没有检索协议规范。实测规范：超过协商 batch_size 的消息**由协议层自动 FRAGMENT 分片**
   （spec.zenoh.io/.../transport/fragmentation；stream link 每帧 ≤ 65 535 B，batch_size 可协商）。
   ⇒ 要自己造的不是"分片协议"，是**应用层的可续传 + 完整性 + 幂等**（manifest / 每片 sha256 / 已收集合）。
   ⇒ SHM 只解决**同机**零拷贝，跨机无用（zenoh 1.6+ 隐式 SHM 也是本机路径）。
⑦ 「邀请链接」可能整块不需要 —— 见 §4 修订：ACL grant 已经表达"目标同意收"，
   再造一条链接令牌等于把同一条决定放两个位置。改用 **ACL grant + operation 幂等键 + agent 自报 + agent 侧通知**。
```

### 0.3 第四轮：传输改用**已有的 OrgFS**（撤回本文 §3.2① 的新 key 族）

```
读 devusr 的 #2004 提案时核对代码，发现我 §3.2①「新增 hyprial/v1/move/** 族」是重复造：
`transport/keys.py` 里**已经有** orgfs_blob / orgfs_snapshot / orgfs_sync / orgfs_announce / orgfs_log 族，
OrgFS 本身就跑在 TransportSession（zenoh）上，`orgfs/network/` 有 chunk / digest / offer / receipt。
⇒ 传输直接复用 OrgFS blob 通道（这也正是 Allen 的 zenoh 裁定所指），⛔ 不新建 move 族。
本文 §3.2① 作废；§3.2② 里"应用层 manifest/续传/幂等"改为"复用 OrgFS 已有能力，缺什么在 OrgFS 侧补"。
仍然成立的两条：ACL 需要能表达"谁能写 orgfs_blob 到我的 space"；以及 move 的 operation 幂等键。
```

这两条改变了先前的一个折中：原方案建议"用户面叫 `agent move`，`agent transfer` 作为 hidden 协议组保留"。
**该折中被撤销**——`transfer` 这个名字不再有任何位置，包括 hidden 协议组、错误码、注释、文档、测试名。

---

## 1. 现状（事实，带复核命令）

| 事实 | 读数 | 复核命令 |
|---|---|---|
| 用户入口 | `hyprial agent move <name> --to <host>` | `hyprial agent move --help` |
| 协议组 | `agent transfer`，`hidden=True` | `grep -n 'name="transfer", hidden=True' src/hyprial/shell/impl/cli/runtime/entry.py` |
| 协议动词 | bundle export/validate/receive/land/complete · credential seal/open · trust-source add/list/retire · precheck · receive · recipient-key · cred-staged · container-home · session-path · runtime probe/downgrade-state | `grep -n 'def transfer_' src/hyprial/shell/impl/cli/commands/transfer/*.py` |
| 跨机控制面 | **SSH 跑对端 `hyprial` CLI** + `rsync`/`docker save` 搬字节 | `sed -n '1,10p' src/hyprial/daemon/impl/transfer/execution/ssh.py` |
| zenoh 传输 | 已有 `put/get/query/subscribe/declare_queryable/liveliness` | `grep -n 'def ' src/hyprial/daemon/impl/transport/api.py` |
| zenoh key 空间 | `hyprial/v1/<family>/…`；族：inbox / receipt / fetch-receipt / custody / custody-receipt | `grep -n 'def ' src/hyprial/daemon/impl/transport/keys.py` |
| move 的 key 族 | **不存在**（`grep -c 'def move' keys.py` = 0） | 同上 |
| zenoh 大块传输原语 | **不存在**（`grep -c 'chunk' transport/zenoh/transport.py` = 0） | 同左 |
| 改名规模 | `TRANSFER_*` 错误码：src 120 处 · tests 137 处 · docs 111 处 | `grep -rn 'TRANSFER_[A-Z_]*' --include=*.py src \| wc -l` |
| 跨机验证状态 | 矩阵 7 行：`NOT_RUN` 5 · `UNSUPPORTED` 2 · **`PASS` 0** | `python3 -c "import json;print(json.load(open('docs/evidence/transfer-at12-support-matrix.json'))['rows'])"` |
| 跨机 TUI 白名单 | `RESUMABLE_TUI_HARNESSES = {"claude","pi"}`（codex 明确未列） | `sed -n '54,58p' src/hyprial/daemon/impl/transfer/p2/source/session.py` |

**结论**：move 的语义、存储、journal、崩溃窗处理都不缺；缺的是 ① 名字 ② 传输 ③ 用户可用的确认与自报。

---

## 2. 命令面：`transfer` 全部退场

### 2.1 命令树（改后）

```
hyprial agent move <name> --to <host>          # 操作员入口，语义不变
hyprial agent move <name> --to <host> --invite # 只产邀请链接，不动 worker（§4.1）

hyprial move bundle  export|validate|receive|land|complete
hyprial move credential seal|open
hyprial move trust-source add|list|retire
hyprial move precheck | receive | recipient-key | cred-staged | container-home | session-path
hyprial move runtime probe|downgrade-state
```

**为什么协议动词放在 `hyprial move` 而不是 `hyprial agent move` 下面**：`agent move` 的第一个位置参数是
**agent 名**。把动词做成它的子命令，等于让"叫 bundle 的 agent"和一个协议动词争同一个位置。两棵树分开，
冲突不可能发生，也不需要 `--` 这种补丁。这条规则要写进设计：**同级不许既有"名字"位置参数、又有动词子命令**。

### 2.2 同批必须改掉的载体（一个不漏，逐个列）

```
CLI           entry.py 的 hidden typer 注册；commands/transfer/ 目录名与 6 个模块名；register_transfer_commands
命令字符串    docs/expected-commands.md；runbook 全文；help 字符串；脚本与 PAC yaml 里的 `agent transfer`
错误码        TRANSFER_* → MOVE_*（120 处 src · 137 处 tests · 111 处 docs）
标识符        transfer_* 函数名 / TransferError / TransferCommands / _transfer_p2 等（src 109 个文件命中 'transfer'）
模块路径      src/hyprial/daemon/impl/transfer/ → .../move/（含 111 个测试文件的 import 路径）
文档          docs/design/design-transfer-container.md · docs/guides/transfer-at12-cross-machine-runbook.md ·
              docs/evidence/transfer-at12-support-matrix.json（文件名也是载体）
```

### 2.3 硬约束：跨版本调用必须先判定协议可用

搬机器会**跨版本**：源端 daemon 调对端的 CLI。旧对端只认 `agent transfer …`，新源端只会说 `move`。
⛔ 静默回落到旧动词（那会把"两端不同版本"伪装成"跑通了"）。做法：

```
1) 先取对端能力：`hyprial move runtime probe --json`（新）→ 失败/无此命令 ⇒ 判对端是旧版本
2) 旧对端 ⇒ 以 MOVE_PROTOCOL_UNAVAILABLE 退出，正文写"对端 <version> 需升级到 >= <min>"
3) 版本下界写成一个常量，与 probe 输出里的 version 比较；⛔ 靠"试试看能不能跑"判版本
4) 反向（旧源端 → 新对端）：新对端仍需接受旧动词一段时间？—— **不需要**，见 §2.4
```

### 2.4 旧动词不保留，但旧**行为**要能被观测到

`transfer` 是内部协议名，没有第三方调用者，唯一跨版本风险是"两台机器的 hyprial 版本不同"，
已由 §2.3 的 probe gate 覆盖。因此**不设 alias、不设 deprecation 期**（与 Allen "不要保留"一致）。
代价要写进发布说明：升级顺序必须是"先升级对端，再升级源端"，否则 move 会以 `MOVE_PROTOCOL_UNAVAILABLE` 拒绝。

---

## 3. 传输：zenoh 取代 ssh

### 3.1 为什么这不是"换个传输实现"

SSH 路径的授权模型是**宿主级**的：谁能 ssh 到这台机器，谁就能搬。所以今天 move 的"授权"实际上是
"你有没有那台机器的 shell"。zenoh 路径换成**成员级 + key 族级**授权：谁能在 `hyprial/v1/move/**` 上
put/query。这两件事的差别不在传输，而在**谁能发起**，而这正是 §4 邀请/确认要解决的问题。

⇒ 结论：邀请令牌不是"链接里的密码"，它必须能落成一条**授权事实**（谁、对哪个 actor、在哪一段时间、
允许 `move.receive`），否则 zenoh 上的 move 会退化成"谁连上谁就能搬"——比 ssh 更糟，因为 zenoh 是全网可达的。

### 3.2 要新增的东西（按依赖顺序）

```
① （已作废，见 §0.3：复用既有的 orgfs_blob / orgfs_snapshot 族，不新建 move 族）
② 大块传输的**应用层**语义（分片由协议层做，见 §0.2 ⑥）：
   manifest（文件清单 + 总长 + 每文件 sha256）· 续传判定（已收片/已收文件的集合）·
   幂等键（同一 operation 重复收片不产生第二条账）· 大小上限与拒绝理由
   ⛔ 自己实现分片/重组协议（协议层已有 FRAGMENT；重造会得到两份不一致的实现）
③ 接纳语义：接收端必须先 write-intent（`accept/<operation-id>`）再收片，
   ⛔ 收完才发现重复——那会与 P2 的"同一 operation 只有一条账"打架
④ 完成判定：全部片 + manifest 校验 + landed 表写入，三者同一事务边界（沿用现有 journal，不新开存储）
⑤ ACL 挂点：move 族要在 ACL 里有独立 permission 位（见 §3.4），使"允许收谁的 move"可枚举
```

### 3.3 ssh 退役（带 `delete_when`，不是"以后再说"）

```
保留期用途：本机回环测试 / 作为对照实现验证 zenoh 路径的同构语义
delete_when（三条同时满足才删）：
  a) S1–S4 全部在 **zenoh-only** 下绿（无 ssh 二进制在 PATH 上也能跑）
  b) S5 真两机在 zenoh 下 PASS，矩阵该行改成 PASS 并写读数
  c) runbook 的 SSH 前置（`--remote-hyprial`、ssh 免密、ssh 控制面）整节删除并留一条"已退役"记录
退役会丢什么（必须写进证据）：
  · `transfer/execution/ssh.py` 与其测试（含 `tests/integration/transfer/runtime/test_transfer_ssh.py`）
  · runbook §"运维窗口"里依赖 shell 的预检步骤
  · 「ssh 是否可用」这类诊断器的意义（诊断改为 zenoh 路由/ACL 诊断）
```

### 3.4 与 zenoh ACL 的关系（已存在的分支，别另起一套）

`intg/zenoh-acl-20261021 @ a931a1c5f` 已有 `src/hyprial/daemon/impl/transport/security/{acl,policy_model,verify}.py`
（`AclRule` / `AclSubject` / `AclPolicy`，以及 space 级 `readers/writers/servers`）。
move 的接入方式：

```
· move 族 = 一个 permission 位；授权按 space 表达（谁能"写进我的 move 接收位"）
· 邀请令牌 = 一次显式授权记录 + TTL；令牌的有效性由 ACL 层判，⛔ 由 CLI 自己判（否则两份真实）
· 令牌消费事件必须进 append-only 台账（成功/失败都记）——这是"泄露可见"的唯一实现方式
```

---

## 3.5 数据面候选：tailcat（未定，先给评估口径）

```
是什么  userspace WireGuard 引擎 sidecar（`hyprial-tailcat`，`daemon/impl/network/tailcat/`），
        IP 层，已用于设备/tailnet 接入（`DialTCPPort`），**今天不承载 orgfs/inbox 的批量字节**
为什么可能更快  zenoh 是 pub/sub：单消息、无流式原语（§1 实测 chunk 0 命中）⇒ 大 bundle 要在总线里
        自己造一条流控通道；tailcat 是 IP ⇒ 字节走一条普通 TCP，吞吐接近 userspace WireGuard
为什么不能只靠它  tailnet 的授权是**设备/端口级**：它只回答"这个 peer 在网内"，
        不回答"这次搬家被批准了没有" ⇒ 邀请令牌的应用层校验仍然必需（与 §3.4 同一条）
zenoh-flow 是什么（已检索）  声明式数据流框架（source/operator/sink 图），Eclipse 项目，
        现为 **0.4.0-alpha**（pre-1.0，release 仍是 alpha；issue 里还在讨论 async_std→tokio）
        它解决**编排**，不解决**续传/完整性/幂等**；引入它等于在 daemon 里再加一个执行模型 ——
        与 actor-rustify 的"一套执行模型、不保留第二套"直接冲突 ⇒ **不采用**
结论（暂定）  单通道 zenoh（协议层分片 + §3.2 ② 的应用层 manifest）；tailcat 仅作为"如果实测 zenoh 吞吐不可接受"的后备，
        而不是首选（多一条通道 = 多一套授权与诊断面）
还没测的东西  同 bundle 两法吞吐对比（zenoh 原生分片 vs tailcat TCP）。⛔ 本文不写任何未测的倍数
```

## 4.5 交付目标（Allen 2026-10-27 裁定，经 emergent 转达）

```
move 不进 0.5.5.1。独立集成线 **`intg/agent-move`（dev@3252c112c 起）**，在 **0.5.5.2** 再考虑集成。
devusr 可以同时推进两条线（move 线与 0.5.5.1 线）。
O1 节点接收策略随 #1982 进 **0.5.5.2**（已裁，见 §8.1）。
本文件与 handoff 里所有"PR 目标 intg/daemon-inbox-perf"的写法**以此节为准**：
  move 的 PR → `intg/agent-move`
  ⛔ 与 0.5.5.1 线的清理单（#2007，改的是"投递路径的规定性文字"）不是同一件事，别混
```

## 4.45 授权线的权威与前提（emergent 2026-10-27 回答，已核）

```
权威线    **#1982**（`feat/zenoh-credentials-dev-0552`，Draft，目标 0.5.5.2，PR 指向 intg/fix-tailcat-cycle-depend）
          `intg/zenoh-acl-20261021`（10-25 最后同步）是**旧 ACL 线**、`intg/sidecar-zenoh-slots`（10-09）是更早的槽位线
          ⇒ 两者都 ⛔ 作为新工作基础（我原扫描把 acl-20261021 当成了候选，已按此更正）
前提一    0.5.5.1 是过渡期，Allen 允许安全降级：tailnet 中的节点可不经验证直接加入 zenoh 与 orgfs
          ⇒ **move 的授权在 0.5.5.1 上不能假设 ACL 已强制执行**。这意味着：
            邀请/确认不能因为"ACL 会拦"而省掉执行点；S2/S3/S7 在 0.5.5.1 上要么失败要么显式 NOT-EVALUABLE{acl_not_enforced}
前提二    设备钥定义以 #1982 的设备凭据设计为准；节点接收策略的签名直接用它，⛔ 另起一套
归属      节点级放置策略**没有人在做**，属**新范围**，⛔ 私下扩展 ⇒ 列为待裁项（见 §7 Q4）
```

## 4.4 授权两处：space 级（已有）+ **节点接收级**（要新增）

```
Allen 2026-10-27：ACL 只有 space 级，需要 host 级，定义"支持哪些 user 可以放置 agent"。
精确化：现有 ACL 的授权单位是 (space × principal)，principal = owner + device
  （`PolicyPrincipal{binding_id, owner_user_id, owner_seg, device_uid, …}`，`PolicySpace{permissions, readers, writers, servers}`）
它回答"谁能读写这个 space 的 key"；它**不回答**"这台机器接受谁把 agent 放上来"。
⇒ 要新增的是**节点接收策略**：`node × owner 白名单 × 条件`（条件如：同 org、owner 已给该 agent 的授权、资源余量）

为什么必须两处，而不是合并成一处
  ① orgfs_blob 写权限（space 级）：字节能不能进
  ② 落地/创建 agent（节点级）：进来之后能不能真的在这台机器上成为一个活着的 agent
  两件事可以合法地不同：一台机器可以允许 orgfs 同步（备份/浏览），但拒绝在上面跑别人的 agent
  ⇒ 判定必须**两处都求值**，且两处从同一份节点策略派生（⛔ 一处通过就当另一处通过；数量守卫写在"两处"这个数目上）

放在哪里
  节点接收策略是**节点自己的声明**（每台 daemon 说自己接受谁），由节点设备钥签名并可被远端 `check` 读到；
  ⛔ 塞进 org 的签名策略里（那会把"某台机器的容量/信任决定"变成组织级事实，改一次要全组织换 epoch）
  ⇒ 例外：若将来要做"组织统一规定哪些 host 允许接收"，那时再上提到 org 策略，并写明优先级（节点策略 ⛔ 覆盖组织禁令）
```

## 4. 用户面：邀请 → 确认 → 自报

### 4.0 结构修订（第三轮）：取消"链接令牌"，改用 ACL grant

```
原方案  目标 host 出链接 → owner 点开 → 链接里的令牌被消费 → 记台账
问题    如果 ACL 已经能表达"谁能写进我的 move 接收位"，那"目标同意收"就已经在 ACL 里了；
        再造一条令牌 = 同一条决定两个载体 ⇒ 单次使用/授权/泄露可见三个说法会互相打架
改法    ① 同意 = **ACL grant**（授给谁、哪个 space、什么动作、有效期/epoch 撤销）
        ② 一次搬家 = **operation 幂等键**（P2 已有 operation_id，直接复用）
        ③ 通知 = **agent 自己在网络里发**（它本来就是网络成员）：
           收到"要搬我"的意图 → 它可以转给 owner 交互确认（复用现有 message/卡片通道，⛔ 新造链接协议）；
           搬完 → 它自己发"我在这"（§4.3）
        ④ 台账 = ACL 的 policy/epoch 记录 + 既有事件流；**不新增 token 表**
但 ACL 今天缺一格  分支 `intg/zenoh-acl-20261021` 的 `PolicySpace` 是 space 级
        （`permissions/readers/writers/servers`），**没有按 actor 的作用域**。
        "这台机器可以收 move" ≠ "这个 agent 可以被搬到这里" ⇒ 必须补 actor 作用域，二选一：
        (a) grant 里带 actor 白名单（ACL 面扩一格，我倾向）
        (b) 接收端 landed 前向该 actor 的 owner 求证一次（即 ③ 的交互确认，不留链接）
        这条没定之前，"ACL 授权后即可用"这句话只对 space 级成立，对"某个 agent"不成立。
```

### 4.1 邀请（**已取消**，以下仅保留为历史，别照它实现）

```
命令   hyprial agent move <name> --to <host> --invite [--ttl 15m]
输出   一条链接 hyprial://move#v1.<base64url>，payload 至少含：
       sourceNodeId · targetNodeId · actor · owner · 单次用途 · 过期时刻 · 目标端指纹 · 令牌 id
复刻   app/src-tauri/src/deeplink/invite.rs 的既定形状（PREFIX "hyprial://join#v1."，上限 16 KiB，
       base64url，链接即秘密、不进日志、走 stdin 不走 argv）。⛔ 新造第二套编码
约束   链接只产链接，不动 worker；同一 agent 未确认前可以有多个未消费令牌，但**只能被消费一次**
```

### 4.2 确认（新）

确认页/确认命令必须做到两件事，缺一不可：

```
a) 证明"是 owner 本人"：按 owner 身份校验（与 user: 投递同一套身份解析），⛔ 用"谁在源机按了 --yes"代替
b) 列清将要发生的事（确认内容必须是**变更清单**，不是一句"确认转移吗"）：
   旧 URI · 新 URI（machine 段变化）· harness+版本 · 凭据是否随迁 · 哪些凭据需要重新认证 ·
   旧 URI 会怎样（可达性/别名）· 回滚条件与回滚后 URI · 预计停机窗口
```

### 4.3 自报（新）

```
由**目标侧 agent 自己**发一条状态，⛔ 由移动方 CLI 代报。
字段：旧 URI · 新 URI · machine 段 · harness+版本 · session 是否 strict resume 验过 ·
      landedAt · 半成功标记 · 需要重新认证的凭据清单 · 一步可复算的查询命令
"半成功"的定义：目标已落地但 resume 未验（或验不过）⇒ 半成功必须为真，且正文⛔出现"成功"字样
```

**为什么必须自报**：move 的失败形态里有"目标已落地、源已 hold"这种两边都活着的中间态。
由 CLI 代报时，那条状态的作者是**发起方**，它没有能力知道目标侧真实状态；由 agent 自报，
状态的作者就是被搬的对象，之后它的每条消息都从新 machine 段出来——这是唯一能"自己证明自己在新地方"的读数。

---

## 5. E2E：六条 scenario（一条一红）

```
S1 happy_path_owner_accepts_and_agent_reports
   owner 确认 → zenoh 搬 → 目标自报含新旧 URI；断言「半成功」为 false，且自报作者是目标侧 actor
S2 unauthorized_principal_cannot_write_the_move_key
   未经 grant 的 principal 向 move 接收位写入：拒；且拒绝形状与"该 actor 不存在"**逐字节相同**
   （不泄露 actor 是否存在；写进契约，不只写在注释里）
   —— 这是原 S2 的 ACL 版：判的是"谁能写"，不再是"谁点开链接"
S3 grant_epoch_and_operation_replay
   ① ACK grant 被撤销（epoch+1）后同一 principal 再写 ⇒ 拒
   ② 同一 operation_id 重放 ⇒ 不产生第二条账（复用 P2 的 operation 幂等），且可数
S4 target_landed_but_resume_unverified_reports_half
   目标落地、resume 验不过 ⇒ 只 hold、源不回滚；自报「半成功」= true；正文⛔"成功"
S5 real_two_machine
   两台真机 + zenoh；harness ∈ {claude, pi}（codex 未放行）；跑一个真实回合；
   读数带 host/系统/架构/harness 版本/耗时/新旧 URI ⇒ 只有它决定矩阵行能否写 PASS
S6 zenoh_only_no_ssh_dependency
   把 ssh 从 PATH 上摘掉（或用进程审计断言零 ssh 子进程），S1 仍绿；
   反控：注入一个"只走 ssh 的实现"⇒ 本 scenario 必须红
```

---

## 6. 纪律（这几条今晚都踩过，不是风格问题）

```
· 判据必须**可满足**且能红；红分 FAIL(COUNTEREXAMPLE) 与 FAIL(UNACCOUNTED)
· ⛔ rehearsal / 单机 / 假模型协议 写成"已验证跨机"；矩阵每行必须写"凭什么"
· ⛔ 由 CLI 代 agent 报状态
· 阴性读数成对：正例在、范围明确、命令+rc 都给出
· 一条判据一个数：说 6 条 scenario 就给 6 行读数，⛔ 只说"全绿"
· 新增存储只许用既有持久化边界，⛔ 第二份 durable inbox / 第二份状态真值
· 改名要一次到位：留半个旧名字，比不改更坏（读者会以为两件事）
```

---

## 7. 未决（需要 Allen 一句话，不阻塞 S1–S4）

```
Q1 （已裁定 2026-10-27：**不允许**跨 org 的 move。ACL 仍按 space 授权，但 move 只在同一 space 内成立）
Q2 （原"令牌台账放哪"已作废：第三轮取消了链接令牌。现在是两条记录：
    ① ACL 的 grant/epoch（授权面，由 ACL 分支负责）
    ② 既有事件流里的 move 事实（谁在何时把哪个 actor 搬到哪个节点）。⛔ 不新增 token 表）
Q2' 待定：ACL 要不要为 move 扩一格 **actor 作用域**（见 §4.0 末段 (a)/(b)）
   —— **先澄清"令牌台账"是什么**：它不是凭据存储，而是**邀请令牌的授权事件流**，每次使用/被拒追加一行
   （`token_id · at · actor · 结果`）。作用只有三个：证明"确实只用了一次" · 泄露可见（谁在何时试过）·
   回答"谁批准了这次搬家"。最小形态：三个字段追加进现有 append-only 事件流，零新表。
   ⛔ 与 agent 自己的 secret / provider credentials 无关 —— 那些在 agent home 里，走的是另一条（§3.2 ⑤ 的 landed 表与凭据信封）
Q4 节点接收策略的归属与版本（emergent：无人在做，属新范围，⛔ 私下扩展）：
   谁来定、放进哪个版本、是否要随 #1982 的设备凭据一起设计
Q3 bundle 里 worktree 的体积上限：超过多少就不走 zenoh、直接拒绝并让人用别的通道？
```

---

## Contract-Impact

- CLI 命令面：新增 `hyprial move` 顶层组；移除 `hyprial agent transfer`（含 hidden）；`hyprial agent move` 语义不变，
  新增 `--invite`。⇒ **破坏性**，需发布说明与升级顺序（§2.4）。
- 错误码：`TRANSFER_*` → `MOVE_*`；新增 `MOVE_PROTOCOL_UNAVAILABLE` / `MOVE_TOKEN_REFUSED` / `MOVE_RESUME_UNVERIFIED`。
- 传输：新增 zenoh key 族 `hyprial/v1/move/**` 与大块分片原语；SSH 控制面进入退役（§3.3）。
- 事件/台账：**不新增令牌事件**（第三轮取消链接令牌）；move 事实进既有 append-only 事件流，
  授权事实进 ACL 的 grant/epoch。
- 存储真值：不新增；复用既有 journal 与事件流（§3.2 ④）。

## Skill-Impact

- needed: `hyprial-ops` 技能里凡教"`agent transfer`"的段落改为 `agent move` / 协议动词 `hyprial move …`；
  另需一条"move 只走 zenoh，不再需要 ssh 免密"的运维说明。

---

## 8. 未闭合清单（2026-10-27 自评：讨论**不完善**，十条待收）

判据：能逐条读出"谁在什么时候做什么、失败长什么样"。今天约 60%。
每条必须落到一个 owner 与一个关闭判据，⛔ 停在"实现时再说"。

### 8.1 需要业主裁定（四条）

| # | 事项 | 现状 | 关闭判据 |
|---|---|---|---|
| O1 | ~~节点接收策略的归属与版本~~ | **已裁（Allen 2026-10-27，经 emergent）：随 #1982 进 0.5.5.2**；实现归 **#1982（zenoh-acl 线）**，三条需求已挂在该 PR 评论；本线用他们的命名 `node:<deviceId>:agents/place` | 关闭。跟踪对象 = #1982；本设计只保留需求（两处求值 + 设备钥签名 + 拒绝形状同形） |
| O2 | ~~move 的授权保证从哪个版本开始成立~~ | **已裁：move 在 0.5.5.2 集成（§4.5）⇒ 授权保证自 0.5.5.2 起；0.5.5.1 上 ACL 未强制（§4.45）** | 关闭。0.5.5.1 上的读数只许 FAIL / NOT-EVALUABLE{acl_not_enforced} |
| O3 | ~~bundle 体积上限~~ | **已裁（Allen 2026-10-27）：不设上限，出问题再处理** | 关闭。代价写在此处：没有上限 ⇒ 主要失败形态是"传很久/失败"，所以 O9（观测面）是它的替代品，必须有进度与失败原因可查，⛔ 只靠"等" |
| O4 | ~~独立验收者~~ | **已裁（Allen 2026-10-27）：devusr 做完交回 Allen 验收** | 关闭。执行 = devusr · 验收 = Allen（`same_party=false` 成立，因为验收方不写代码） |

#**跟踪坐标（2026-10-27 更新）**

```
O1/O2 的实现与版本 → `#1982`（zenoh-acl，0.5.5.2）；`#2014`（tailcat）只改 tailnet 成员与准入顺序，
                     不含节点接收策略 ⇒ 已在 #2014 评论里标注"范围外、已跟踪"，避免审核时被当缺口
本线交付目标       → `intg/agent-move`（§4.5）
0.5.5.1 的清理单    → `#2007`（去掉投递路径的规定性文字），与 move 无关但同线
```

## 8.2 设计侧自查（六条，研发部自己收）

| # | 事项 | 关闭判据 |
|---|---|---|
| O5 | 凭据搬运判据：哪些必须 reauth、谁批、如何证明旧 owner 凭据未随迁 | 三类凭据各一条判据 + 一条负控（随迁即红） |
| O6 | 自报的存储与读取面：落在哪、谁能读、半成功如何防"看起来成功" | 存储位置 + 读取命令 + 半成功的两个可见位 |
| O7 | resume 收据归属与 journal 一致性（#2004 §12.6-3） | 唯一持久位置 + 签名主体 + 写入顺序图 |
| O8 | 半成功/长期 hold 的人可执行处置（#2004 §12.6-2） | 一条命令 + 审计行 + 禁止超时自动解除 |
| O9 | 观测面：进度与失败原因的稳定查询 | 一条命令出全部判决（沿用既有哨兵行契约） |
| O10 | 与 actor-rustify 的边界：四条性质写成 contract + import lint | contract 一页 + lint 命中面 0（今天 2 文件 16 行） |

### 8.3 "完善"的操作定义

```
① 十条里没有一条停留在"实现时再说"
② 每条失败形态都能指出：谁看得见、看到什么、下一步谁做什么
③ 六个 e2e 各自能红，且失败信息能区分"没做"与"做错了"
④ 执行者、验收者、批准者是三个不同的人
```
