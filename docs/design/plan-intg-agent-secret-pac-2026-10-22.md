计划：`intg/agent-secret` 的完整 PAC（终点 = 并入 dev）  2026-10-22

状态：**计划交付，未启动**。本文件不是「PAC 已建」的声明。规格见
`docs/design/spec-agent-secrets-sqlite-2026-10-22.md`（PR #1866，base `intg/agent-secret`）。

## 0. 终点与判据

```
终点      intg/agent-secret 合入 dev
判据 A    规格 §6 的九条验收在 CI 里可红可绿（修复前能取到假值）
判据 B    §5.5 撤销语义在实现中可验：撤销后值不在盘上；只做 revoke_remote 不删值的
          「未完成态」能被检出，且事件推导与表推导给出同一答案
判据 C    威胁模型（同 uid 是边界）与「validator 只做形态」两条入档
判据 D    非模型凭据可经命令面交付：Cloudflare/R2 与 Forgejo 各走通一次 setup → 注入 → 使用
```

## 1. 工作包

| WP | 内容 | 面 | 依赖 | 能红的判据 |
| --- | --- | --- | --- | --- |
| WP1 | 身份面：删 `SECRET_ENVIRONMENT_NAMES` 枚举；保留名拒写；保留「源 entry 持有者」判断 | `identity/impl/agents/runtime/secrets.py` | — | 保留名写入被拒并留事件；非模型名可写 |
| WP2 | 存储层：`secrets.sqlite` + `secret_events` + 两条 append-only 触发器 + fingerprint 规则 | 新模块（identity 或 agent home 侧） | WP1 | 验收 1/2/3：值不进事件；UPDATE/DELETE 被 ABORT；指纹变化可见 |
| WP3 | 注入面：`registry_ops` 注入点改读 sqlite；写 `materialize` 事件；无 `set` 事件的行不物化 | `application/actors/agents_admin/registry_ops.py` | WP2 | 验收 5/6：SQL 直插行被拒；保留名不注入 |
| WP4 | transfer 落地侧同批改：同一凭据不得有两种判断 | `transfer/landing/credentials.py:351` | WP1 | 两家对同一 grant/行给出同一结论（成对断言） |
| WP5 | CLI 面：`agent secret set/rotate/remove/history/list`（值只走 stdin） | `shell/impl/cli/commands/agent/secret.py` | WP2 | 验收 7：无 validator 默认拒绝；`--no-validate` 成功且计数 +1 |
| WP6 | 迁移与文档：owner 逐条导入（不自动搬迁）；威胁模型与撤销语义入档 | docs | WP1–WP5 | 导入产生 `actor=user:<owner>` 的 set 事件 |

## 2. 序列

```
WP1 ──┬─→ WP2 ──→ WP3 ──→ WP5
      └─→ WP4
WP6 在 WP1–WP5 之后
```

WP4 只依赖 WP1（它只用「谁可以持有」这一格），因此可与 WP2 并行。
WP5 需要 WP2 的表已定，但不需要 WP3 的注入路径，可与 WP3 并行开发、最后统一验收。

## 3. 验收汇总（在 dev 合入前必须全绿）

```
§6 九条        逐条对应到 WP1–WP5，不允许"由集成测试代替"
§5.5 撤销      WP2 提供触发器，WP3 提供物化判定，WP5 提供 remove 动作
route_delivery 负控    已有实测（operator/session 两种策略均 REFUSED other-agent-home）；
                       端到端一次真实主动投递仍 UNTESTED，列为 WP6 的收尾项
```

## 3.5 读数与状态：五条（hq-adjutant 2026-10-22；来源散见下注，合并成一节）

> 六节元规则围一个 WI，读者通常一条都不照做。以下五条是同一件事的五个面。

**1. 状态词必须可观察。** 「已完成 / 已废弃 / 正常」只有在能被指出一个观测时才是一个状态；
否则它会被读成"作者想说的那个意思"。

同一条对**期望值**也成立（hq-adjutant 2026-10-23 补）：

```
期望值必须能随观测更新,⛔ 把历史读数当永久标准
实例:我把"log 应见 7 条"写进冻结的解除条件 —— 7 是某次实测,
      purge 后若只回来 5 条,这条判据会让冻结**永不解除**
⇒ 写"应为"时,同时写它的**来源时刻**;并给出"若达不到,记什么状态"的出路
```

```
可观察      "log 已 7 条，末条内容为 …"
不可观察    "已确认 / 已验证 / 基本对 / 我读回了"
```

**2. 一次 org 写入有四个状态，⛔ 压成两三个。**

```
submitted                   请求已发
write-receipted-unverified  daemon 回执里含该条目（第 1 层），未独立读到
verified                    独立读回，条数对且逐字对（第 2 层）
verified-mismatch           独立读回了，但内容不是那一条（写入/覆盖问题，⛔ 观测缺口）
```

`unverified` 与 `mismatch` 必须分开：合并后恢复复核会看到"条数对、内容非空"而记成
`verified` —— **复核本身产出假绿**。

**`verified` 不是永久状态**（hq-adjutant 2026-10-22 实测补的第五态）：

```
verified-then-evicted   曾是 verified；后来存储把它清掉了（daemon 重启 + 历史 purge）
⇒ 本 WI 实例:log 曾 7 条 verified；daemon 换 pid(4742→20133, epoch ed0614de) 后
   读出 log=0、item 只剩 5 键、invalid=true、reason=history incomplete after purge,
   而命令仍返回 ok:true（8/8,5.1-8.4s）⇒ **成功 + 空对象 = 假绿**
⇒ 判据:verification 是三元的 —— (内容, 时刻, 存储),⛔ 一个布尔
   "verified at T on storage S"；S 被 purge 之后,该结论自动降级为 invalid
   ⇒ 重新验证**只能靠独立重放那条命令**,⛔ 靠"我之前验过"
```

⇒ 同一件事的写法：**验证有保质期，且保质期由存储决定，不由时间决定**。

它的 2×2 成因（**仅用于解释，⛔ 不可实现**；实现见上面的四态 + 下面的判定表）：

```
                 可判定            不可判定
内容相符         verified          undefined
内容不符         verified-mismatch undefined
```

轴是「可判定 / 不可判定」，⛔「内容是它 / 不是它」。没读到时内容轴是**未定义**，写成
"不是它"＝一次没有证据的指控。右列两格都是 `undefined`，所以这张表**不是分区表**
（分区表要求互斥且穷尽）；照它写 if/else 必然把 `undefined` 折回某个分支 —— 那一折就是
把"没观测"当成"观测到不符"。

复核判定表（**可执行**）：

```
命中数 == 1                      ⇒ verified（允许其后有追加）
命中数 >= 2                      ⇒ `ambiguous`：取**最近一条**，并写明共命中 N 条
未命中 且 条数 < 最后已知         ⇒ verified-then-evicted
未命中 且 条数 ≥ 最后已知         ⇒ verified-mismatch，须回报（⛔"基本对"）
读不到（超时/失败/空对象）        ⇒ write-receipted-unverified，⛔ 判失败也⛔ 判成功
```

**两个 corner（hq-adjutant 2026-10-23 补）**

```
① 指纹可能不唯一：同一段文本写两次 ⇒ 两条同样内容、不同时刻的记录
   ⇒ 「命中」只证明**存在**这样一条,不证明**我那条**还在
   ⇒ 所以要求 **命中数恰好 1**；≥2 走 `ambiguous`（取最近、写明 N）
② 「时刻」必须是**记录自带的 ts**，⛔ 本地写入时间
   ⇒ 否则两台/两次的时钟差会把同一条记成两条,或把两条记成一条
```

⇒ 判据一句话：**「命中」是存在性，「恰好一条」才是身份。**

**通则：追加型集合里，「最后一条」不是「我的那条」。**

```
log / 事件流 / 审计表都是追加型：你写完就有别人在你后面写
⇒ 判据必须按**身份**找（内容指纹、稳定 id、时刻),⛔ 按位置（最近一条、第 N 条）
⇒ 位置型判据在追加发生的那一刻开始误判,而且方向是**假红**（它报"内容不对"）
```

**3. 判据三属性（缺一个就只是装饰）。**

```
可分离实施   该层能在不完成另一层的情况下真的实现，否则检验跑不起来
可外部观察   "有意义的中间状态"要能写成一句外部观察者看得到、且读起来是合理完成态的话
             反例：「读了库没写事件」—— 没人想要它，它就是缺陷本身，不是"一半"
双向缺一检验 去掉它 ⇒ 只有它红（其余仍绿）；加回它 ⇒ 只有它绿（其余不变）
             单侧不够：只做"去掉它变红"会被「未测」跑绿（未测也红）
```

**4. 拆层的判别式：拆的是"独立子目标"，不是"合取的两半"。**

```
问：「A 通过而 B 未通过」是不是一个有意义的、可能出现且值得单独记的状态？
   是 ⇒ 两个独立子目标，拆
   否 ⇒ 同一目标的两个必要测试，⛔ 拆（拆了得到"两个绿一个没验"）
```

```
identity 面「删枚举 / 保留名 / 持有者判断」    ⇒ 拆
CLI 面     「写面 / 读面」                     ⇒ 拆
launch 面  「读 sqlite + 写 materialize 事件」  ⇒ **不拆**（只读不记正是要修的缺陷本身）
```

**5. 报告与停止条件。**

```
commit message  用 `git commit -F <file>`，⛔ 内联 -m 带反引号（会被 shell 当命令替换吃词）
                 回读 `git show -s --format=%B` 确认整段落盘（hq-adjutant 2026-10-23）
```

```
报"已改"   必须带 分支 + sha（例：docs/spec-agent-secrets-sqlite @ c73d8661c）
验提交     用 git show <sha>:<path>，⛔ 读工作树
验能跑     用干净 worktree（git worktree add），⛔ 在共享树上切分支
          —— worktree 的 .git 是**绝对路径指针**：⛔ /tmp（会被清，且与 tmp 清理撞车）、
             ⛔ agent home 下（transfer/归档即断）、✅ 节点级固定目录
             （本仓约定 ~/Workspace/HyprialOS/wt/）、✅ 推走后 git worktree remove
             ⚠️ **风险不是一个目录会被删，而是一次前缀判断能波及 275 棵活树**
                · 本机实测（管道内计数）：注册 worktree **653**、其中 **275** 在 `/tmp`
                  或 agent home 下、`prunable` **0** ⇒ 这 275 个全是活树，不是垃圾
                · 清理器目前只按**名字前缀**（`pytest-*`）挑，没有按 worktree 注册表排除
                  ⇒ "275 个没被删"是**运气**，⛔ 设计保护
                · 653 个的 gitdir 都指向**同一棵主仓** ⇒ 主仓一搬，全部失效且不报错
                ⇒ 三者叠加：**一条前缀规则 × 275 棵活树 × 同一个主仓指针**

                **后果序列（hq-adjutant 2026-10-23；⛔ "少 275 个目录"）**
                ```
                ① 275 个目录消失,而注册仍在 ⇒ **275 条 dangling**
                ② 在 prune 之前向同路径 `git worktree add` ⇒ 报 **already registered**
                   ⇒ 脚本连环失败,而错误读起来像"路径被占用"（指向错的方向）
                ③ 目录里的未提交编辑直接丢失（无提示、无备份）
                ⇒ 真正的后果:**275 条注册失去对象 + 新工作无法复用这些路径,直到 prune**
                ```
                **反向提醒**：⛔ 对 275 棵全部执行 `git worktree prune`
                目录还在的会被跳过（本机实测 `prunable=0`）⇒ 跑 prune 什么也不会变,
                只是让人以为"处理过了"
             ⚠️ "节点级"只解决**不随 home 搬走**，不解决**整节点迁移**
差分       必须逐变量：证明"A 无关"要控制住第三个变量再撤 A
          （反例：git stash -u 把未跟踪的 sidecar 一起撤走 ⇒ 变绿不能归因）
计数       在管道里做，⛔ 显示后数：
           可被截断的通道上，任何"N 个"都只是**下界**；检测信号是输出**没有收尾行**
           （本机实例：`git worktree list` 被截到 50KB，我数了可见窗口的 377，真值 653）
           正确写法 `git worktree list --porcelain | grep -c "^worktree "` ⇒ 653
           同法实测：tmp 或 agent home 下 275、`prunable` 0（全是活树）
停止       验证链必须能停在**别人可重放**的地方：
           「记录内容能被一条独立命令重新观测」= 终点（本 WI：任何人跑 org work show 见 log 7）
           ⛔ 终点：「我口头确认过」「我读回了」（第 1 层，无法重放）
```

**共享树纪律**（同属第 5 条）：共享树**没有"正确分支"**，只有**进入时的快照** ——
进来先记三个读数（branch · HEAD sha · dirty），要切先开独立 worktree，真切了立刻报三个读数。

> 上面五条是**永久规范**。临时指令另起一节（§3.6），⛔ 与规范同表 ——
> 否则临时指令解除时，编者删"这一节"，永久规范会跟着消失。

## 3.6 临时指令（**会过期**；解除后本节整体删除）

hq-adjutant 2026-10-22 的取舍：**永久规范与临时指令不能共用一个表头**。五条规范的寿命与
本 WI 同长，而临时指令有起点、有解除条件，混在一起解除时会把规范一起删掉。

```
指令      当 org.list 全失败时 ⛔ 非必要的 org work 写入
          内容先落 notes；恢复后**一次性补并当场复核**；⛔ 逐条试探性写入
触发原因  org.list 间歇 15s 超时（实测 3/5 OK 但 16.5–17.7s；后恶化到 6/6、9/9 全失败）
          每次试探都要等一个 deadline，失败还可能半写入
解除条件（三个都要，缺一不解除；hq-adjutant 2026-10-23 给死数）
          ① 连续 3 次**写成功**，且每次**紧跟一次读回、读回内容与预期一致**
             （⛔ 只看 ok:true —— 本机实测过 ok:true + 空对象）
          ② 这三次跨 ≥ 10 分钟（⛔ 三秒内三次）
          ③ 同一窗口 N=5 次采样：延迟 p50 < 5s 且**零超时**
          ④ **解除前必须能读到记录本体**（**按身份查找，⛔ 按位置**）：
             在我这条记录的 **(内容指纹, 记录自带 ts) 上命中且恰好 1 条** ⇒ verified；
             命中 ≥2 ⇒ `ambiguous`（取最近一条 + 写明共 N 条），⛔ 当 verified；
             **允许其后追加其它条目**（log 是追加型集合，别人写第 8 条是正常行为）
             未命中时的两种，⛔ 合成一种：
               · 条数 < 最后已知条数 ⇒ `verified-then-evicted`（写清少几条、少哪段）
               · 条数 ≥ 最后已知但**未命中** ⇒ `verified-mismatch`（我那条被改/被覆盖）
             `ok:true` 但 item 无 log 键 / log 为 0 条 ⇒ **不解除**（空对象假绿）
             ⚠️ 基准是「最后一次已知」，⛔ 写死的数（7 是我 05:29 的一次实测）
             ⚠️ ⛔ 用"最近一条"（位置型）：追加第 8 条后"最近一条"就不是我那条了
                ⇒ 会判成 mismatch 假红（hq-adjutant 2026-10-23 指出）
反向（重新冻结）
          连续 2 次失败，或任一次读回内容与预期不符 ⇒ 立即恢复冻结，
          并在 WI log 记一条「re-frozen: <读数>」
现状（2026-10-23）  **不可解除**：daemon 重启（pid 4742→20133）后 `show` 8/8 返回 ok:true，
                    而 item 无 log 键（0 条）⇒ ④ 不成立
起始日期  2026-10-23
失效方式  解除后**删除本节**；⛔ 保留为空节（空节会被读成"规则仍在但没人写"）

**解除怎么被发现：只读 canary（hq-adjutant 2026-10-23 提法，四态后拆分）**

```
做法    每次活跃时跑一次只读探测：hyprial org work show <WI> --org group-internal --json
        脚本 ~/.hyprial/agents/hyprial-cli-developer/workspace/canary-org-log.sh
四态    FIXED           ok:true 且 log>0                ⇒ 修复已生效，告警**一次**并附条数
        SILENT-INVALID  ok:true 且 invalid:true         ⇒ 记录被标记不可用（另有原因）
        SILENT-EMPTY    ok:true 且 invalid:false 且 0 条 ⇒ **真的没有记录**
        UNSTABLE        超时 / 报错                      ⇒ 读路径不稳定
        ⚠️ invalid:true 与「invalid:false 但 0 条」不是一件事（前者"被判不可用"，后者"确实没有"）
首读数  epoch 1792735952 SILENT no-log-key（v1 格式）
        epoch 1792736576 SILENT-EMPTY no-log-key（v2 格式，UTC 已记录）
⇒ 作用:把"等它修好"从**轮询**变成**状态转变事件**（只在 FIXED 首次出现时喊一次）
```

**频率是读数，⛔ 声明**（同一条被 hq-adjutant 升为判据）

```
名义间隔  NOMINAL_GAP_S = 900s          ← 只是声称
实测      每次运行记 UTC；报告给 runs_last_6h 与 median_gap_s
判据      没有调度器时（scheduled=no）median_gap **不作达标判断**
          ⇒ 我手动戳三次得到的 median 312s，⛔ 读成"正在按 15 分钟运行"
          ⇒ 要报"已在监控"，必须同时给：调度器存在 + runs_last_6h + median_gap
本机实况  无调度器 ⇒ 实际频率 = "我每次活跃时一次"（已实测:3 次/17 分钟）
          ⇒ 若要真 15 分钟:挂到 infra-ops 已有的每小时循环里（**需他同意**，他在提）
```

**旧格式行要单列**：改成 v2 后，v1 行（`<epoch> <state> <detail>`）的"状态"在另一列；
报告把它标成 `legacy:*` 并给出 `legacy_rows` 计数，⛔ 与 v2 混在一起统计 ——
否则迁移本身会把历史读数读错（本次实测：2 行 legacy）。

## 4. 评审与分工

```
super-tier（gpt-6-astra）评审本计划 + #1866 规格，产出分工与节点切法；
本文件只给工作包边界与依赖，⛔ 指定谁做哪一段（那是分工，不是计划）
```

## 5. 启动所需的三个输入（目前我拿不到）

```
1  建 PAC 图的入口：本机 CLI 版本无 workflow/pac 子命令（hyprial help 无 workflow）；
   需要 dev 版 CLI 的 workflow 命令，或由 backend 代建图
2  图名与节点产物目录约定（沿用现有 wf-<hash> 命名？产物落哪）
3  谁有权把图状态推到 running（我这边只能写文档与 PR）
```

⇒ 在拿到这三件之前，本计划停在「可评审」状态，⛔ 停在「可开工」。

## 6. 明确不做（与规格 §8 一致）

权限模型 / grant 批准流程 / 外部审计落地 / 文件交付 / 跨 agent 共享 / 值加密 / 定时轮换 /
能力探测（`capability_probe` 只在将来另开一格，见规格 §5）。
