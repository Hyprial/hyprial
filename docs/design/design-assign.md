# assign 机制:调研 + 设计 + 实施顺序

- 日期:2026-08-28 · 作者:h2b-developer(Allen 命题,hq-adjutant 转达)
- 状态:**设计待评审;这一稿不写实现代码**
- 位置:**stacked 在 #235(`design/selfdrive-routine`)之上**,不是平行的第二份
- 前置阅读:`docs/design/design-selfdrive-routine.md`(#235)、
  HQ `notes/design-worker-vs-agent-lifetime-2026-08-27.md`

---

## §0 与 #235 的关系(2026-08-28 更新:#235 已合入 dev,本稿改为直接对 dev)

本稿最初 stacked 在 `design/selfdrive-routine`(#235)之上,理由是让 #235 能被独立评审,
并让"接在它后面"成为结构上的事实而不是一句话。**#235 现已合入 dev,所以那层依赖没有了**,
base 改为 `dev`。原文里「本 PR 不能先于 #235 合入」的那句**已经失效**,在此作废而不是删除
—— 它被引用过,而删掉会让读过旧版的人和没读过的人拿到两份互不冲突的文本。

⇒ 而**内容上从来不依赖 #235 合入**:routine 实体早就在 `dev` 的 `src/hyprial/routine/` 里(§A)。
依赖的一直是**阅读顺序**,不是代码。

---

## §0.0 为什么 —— 两笔真实损失,不是概念洁癖

**① 2026-08-25 起的 45 小时空转**(HQ 笔记 §1①)

```
up-gate / wf-log / up-restart 空转 45 小时、28 万次投递、烧穿整周配额
⇒ 它们是 worker,而它们的任务(wave 派单)08-25 就【被 cancel 了】
⇒ ⇒ 🔑 worker 活得比它的任务长,而系统里【没有任何东西认为这有问题】
```

**② 2026-08-28 早上,差一点的那次**(hq-adjutant 亲历)

```
Allen 让 hq-adjutant 清理旧 worker ⇒ 它手上【只有 running=false 这一个判据】
⇒ 核基线才发现那 9 个"看起来空闲"的,是【被这次重启打掉的】,升级前全部 running
⇒ ⇒ 差一点抹掉 3 条开着 PR 的活 + 一个常驻角色(e2e-verifier)
⚠️ 而它判断"任务结束了没有",靠的是【它的分支还开着 PR 吗】
   —— 也就是说:worker 的任务状态今天只能从【别的系统】反推
```

🔑 **两笔是同一个缺口的两面**:
```
①"没人回收" ⇒ 死掉的 assign 没有触发任何事
②"不敢回收" ⇒ 想回收的人没有一个可信判据,只好去看 PR 开没开
⇒ ⇒ 而 ② 比 ① 更能说明问题:**判据不存在时,谨慎和鲁莽都会出事** ——
   保守则留一堆空转,激进则误杀在役角色。这一格【不是纪律问题,是缺一条边】
```

---

## §0.1 ⛔ 必须先说的一格:任务书 §1 与 HQ 笔记 §8.2 结论相反

任务书把这一条作为"已敲定、不用再论证"的输入给我:

```
基数     assign 是【多对多】
回收     一个 actor 没有任何活着的 assign ⇒ 释放它
```

而 HQ 笔记 §8.2(2026-08-27,Allen 在场)论证的恰恰是**这一条不成立**:

> 全部结束才释放 ⇒ 只要有一条长命 routine,它**永远不被释放**
> 任一结束就释放 ⇒ 它会在**还在为另一件事干活时**被杀掉
> ⇒ 两种都坏,而这是仅有的两种

**我的判读:这不是矛盾,是【被取代】,所以我按任务书执行** ——
理由是 §8.2 的第一支只有在"希望 agent 在其 routine 仍活着时也能被释放"的前提下才是缺陷;
而 HQ §6.1 已经给了释放事件:**退役那条 routine**。⇒ 于是"routine 活着就不释放"是**期望行为**。

⚠️ **但这个调和是我做的,不是 Allen 说的**,而且它有真实代价,不该被无声吸收:

```
① mandate 曾经回答【这个 actor 为什么存在】,而且只有一个答案
   ⇒ 合并成单一 assign 之后,【没有任何字段回答这个问题了】
② 🔑 而分类规则在【最需要它的那个 case 上变得二义】:
   任务书写「assign 指向 routine ⇒ agent;指向一次性 workflow ⇒ worker」
   ⇒ 那是一条关于【一条 assign】的规则,而 assign 是多对多的 ——
     一个同时 assign 到 1 条 routine + 3 个一次性 workflow 的 actor,是 agent 还是 worker?
   ⚠️ 而 h2b-developer 今天【正是这个 actor】——
     它是这条机制的动机来源,却落在规则的空隙里
```
⇒ ⇒ 这一格进 §G(悬空问题 G1),给 A/B 两案与我的倾向。**我没有替 Allen 决定。**

**补记(2026-08-28,裁定回来之后):**
```
⚠️ §0.1 这个"取代而非矛盾"的读法,Allen 【没有显式裁定】
✅ 但 G1 的裁定【预设了它】:「通过 workflow/routine 来管理就可以」——
   这句话只有在"assign 指向 routine/workflow、多对多、全死才释放"这个模型里才成立
⇒ 📌 所以我按【被隐式确认】继续,而不写成【已确认】
   若这个读法其实是错的,§B/§C 要重写,而不是补丁
```

---

## §A 现状核查:assign 这条边今天在 hyprial 里有没有

**三态回答:【部分存在,只服务一个生产者,且是瞬态的】。**

### A.1 存在的那一半(附 file:line)

```
src/hyprial/routine/store.py:31
  CREATE TABLE IF NOT EXISTS in_flight (
      routine TEXT NOT NULL,  task_uuid TEXT NOT NULL,
      run_id  TEXT NOT NULL,  target    TEXT NOT NULL,
      started_at_ms INTEGER NOT NULL)
```
⇒ `(run_id, target)` **就是一条 assign 边**:某个 run 把活派给了某个 actor 名字。

⛔ 但它有三个性质使它**不能**直接充当 assign 表:

```
① 只有一个生产者:只有 routine 驱动的派发会写它
   ⇒ 手工 hyprial workflow run、PAC 直接派发、agent create 之后的手工使用,【都不写】
② 键是 task_uuid,不是 actor ⇒ 它回答"这个任务在驱吗",不回答"这个 actor 有活吗"
③ 🔑 瞬态:store.py:243-245 `DELETE FROM in_flight WHERE routine=? AND task_uuid=?`
   ⇒ 完成即删 ⇒ 【一张会被删空的表算不出生命周期状态】
```

### A.2 routine 实体(已存在)

```
src/hyprial/routine/store.py:20  routines(name PK, yaml_text, owner, enabled,
                                      next_due_ms, source_error_streak, outcomes,
                                      created_at_ms, version)
src/hyprial/routine/{cli,ports,registry,schema,service,source,store}.py  —— 均已在 dev
```
⇒ 与任务书给的一致:**只支持 interval,不支持 cron**(schema 见 `routine/schema.py`)。
⚠️ 注意 `routines.owner` —— **这是命名雷区的第三个 owner**(§4 雷区表),新字段不能再撞它。

### A.3 不存在的那一半(附【我查了什么】)

```
⛔ agents 侧没有指向 routine / run 的边
   查了:src/hyprial/daemon/desired_state.py 的 provider 记录、agent create 的落库路径
        (cli.py:3272 @agent_app.command("create"))、以及 `ps` 返回的形状
   ⇒ 均无 run/routine 引用字段
⛔ 没有"actor → 它的 assign 集合"的任何查询面
⛔ 没有任何周期性核对会因为"这个 actor 没活了"而释放它
```

### A.4 未查(明说)

```
· 跨机器情形(assign 的 actor 在别的节点)—— 【未查】
· transfer / 迁移路径与 assign 的交互 —— 【未查】
· MCP / harness 侧是否有平行的归属概念 —— 【未查】
```

---

## §B 数据形状(最小方案)

### B.1 一张表

```sql
CREATE TABLE IF NOT EXISTS actor_assign (
    actor        TEXT NOT NULL,      -- canonical actor URI
    assign_kind  TEXT NOT NULL,      -- 'routine' | 'workflow'
    assign_ref   TEXT NOT NULL,      -- routines.name  |  workflow run_id
    assigned_at_ms INTEGER NOT NULL,
    released_at_ms INTEGER,          -- NULL = 活着
    PRIMARY KEY (actor, assign_kind, assign_ref)
);
CREATE INDEX IF NOT EXISTS actor_assign_live
    ON actor_assign(actor) WHERE released_at_ms IS NULL;
CREATE INDEX IF NOT EXISTS actor_assign_ref
    ON actor_assign(assign_kind, assign_ref) WHERE released_at_ms IS NULL;
```

🔑 **`released_at_ms` 而不是 DELETE**,理由直接来自 §A.1③:
```
删行 ⇒ "从来没有过 assign" 与 "曾经有、都结束了" 【压成同一个观测】
⇒ 而任务书 §1 明写这两者【待遇相反】:前者待命不回收,后者要回收
⇒ ⇒ 所以这两种情况【必须在数据里可区分】,删行做不到
```

### B.2 迁移

```
新表,无回填 —— 因为没有可信的历史来源(§A.1 那张表已被删空过)
⇒ 迁移即建表;既有 actor 全部落入"从来没有过 assign"= 待命 ⇒ 不回收
⚠️ 这是【安全侧】的默认:上线不会回收任何既有 actor
📌 而这正是今早那次事故的反面(§5)——宁可漏收,不可误杀
```

### B.3 ⛔ 不加的字段(以及为什么)

```
⛔ 不在 actor 上加 lifecycle 列 —— 任务书 §1 明写生命周期【算出来,不落库】
⛔ 不加 `owner` 裸字段 —— §4 雷区
⛔ 不加 assign 的优先级/权重 —— 没有需求驱动,而它会立刻引出"谁赢"的语义
```

---

## §B.4 命名:定死,并给理由

⚠️ 任务书列了**三个** `owner`。核查时找到**第四个**:

| 已占用的名字 | file:line | 含义 |
|---|---|---|
| `Agent.owner` | 身份域 | actor URI 里的那个人 |
| desired-state `ownership` | `desired_state.py:41` | connector 管理方式(`managed` / …) |
| `Routine.owner` | `routine/store.py:23` | 这条 routine 的发起人 |
| ⭐ `pinned_owner` | `desired_state.py:78` | **新发现**:容器化 spec 上钉住的身份域 owner(决策 D-D,`application.py:3898`) |
| `binding` | 已实装 | actor ↔ harness 会话 |

⇒ 🔑 **五个名字里有四个带 `owner`。这个模型对命名的容错已经用完了。**

**本稿定死的名字(全部自带限定,无一个裸 `owner`):**

```
表名   actor_assign     —— 主语在前,说明这张表是"actor 的 assign",不是"assign 的表"
列     assign_kind      —— 'routine' | 'workflow';⛔ 不叫 `type`(太泛)也不叫 `kind`(会撞)
列     assign_ref       —— 指向对象的标识;⛔ 不叫 `target`(target 在 PAC 里已指派发目标)
列     released_at_ms   —— ⛔ 不叫 `ended`/`closed`;"释放"是本机制的动词,全文一致
状态   created/assigned/releasing/destroyed —— 任务书给定;⛔ 不引入 `pending`
```
⛔ **不引入 `mandate` / `own` / `bind` / `delegate`** —— Allen 亲裁单一名词 assign(§0.1)。

---

## §B.5 派单门层的观测(A3,2026-09-03;先记不拦)

hq/notes/design-dispatch-always-pac-2026-09-03.md §三② 拍板的门层,观测半边落在
本仓库。语义原样,不新增裁度:

```
写 assign   PAC 派发成功送达即写 workflow assign(E2 已实现:registry 在
            deliver:target 成功回执处 record_workflow_assign,assign_ref=run_id);
            手工 hyprial workflow run 之外的裸派单没有 assign 可写 ⇒ 只能数
definition  派单形状 =「非 workflow 发出 + intent=request + 发件方是协调者/人 +
            收件方是本机 agent」。workflow 派发走 inbox(workflow:{effect_id}
            幂等键),天然不经 message.send,故 message.send 边界即「非 workflow」
事件        daemon.jsonl `dispatch.without_pac(sender, recipient, messageId)`;
            ⛔ 只记不拦、不改投递 —— 先要绕过率的分子,拦不拦等 Allen 定
计数        hyprial ps / hyprial agent top 暴露 daemon.dispatchWithoutPacCount(本 daemon
            epoch 内的内存计数;持久真源是 daemon.jsonl 的事件流)
```

---

## §C 回收的不变式

### C.1 形状:周期性核对,不是收尾步骤

```
每 N 秒(挂 daemon 既有 reconcile 节拍,与 routine 到期检查同一处):
  1. 对每条 released_at_ms IS NULL 的 assign,求值它的来源是否仍然活着
       kind=workflow ⇒ run 处于终态(COMPLETED / CANCELLED) ⇒ 这条 assign 死了
       kind=routine  ⇒ routine 已退役(不在 routines 表 / enabled=0 且被显式退役) ⇒ 死了
     死了 ⇒ 写 released_at_ms(幂等:已有值就不动)
  2. 再求"该释放的 actor":
       曾经有过 assign(表里有行)  AND  没有任何 released_at_ms IS NULL 的行
     ⇒ 释放它
```

🔑 **为什么必须是核对而不是收尾**(正面回应硬约束①):
```
cancel / timeout / crash / kill 四条路【都不执行收尾步骤】
⇒ 而 45 小时那次事故里,那些 workflow 恰恰是【被 cancel 的】
⇒ ⇒ 核对的输入是【run 的当前状态】,而不是【谁记得来通知】
```

### C.1.1 ⛔ 「失踪」不等于「终止」—— 而「查不到」也不等于「不存在」

**两句,而第二句才是防误杀的那句(hq-adjutant 提出,h2b-developer 补第二句):**

```
① 来源【不存在】⇒ 这条 assign 死了
   理由:被 cancel 的 routine 留下了一条"我结束了"的记录,核对读得到;
        而被【直接删掉】的 routine 什么都没留下 ——
        若核对只认"报告了终态",删一条 routine 就留下一个永不回收的 actor
        ⇒ ⇒ 正是 45 小时那个形状

② ⚠️ 而「不存在」必须是【查询确定的不存在】,不是【查询没成功】
   「查无此物」有两种来源,而它们在一个空结果里长得一模一样:
     · routine 真的被删了            ⇒ 该算死
     · 存储读失败 / 迁移中 / 权限问题 ⇒ ⛔ 不该算死
   ⇒ 存储层必须把【确定不存在】与【这次没查成】分开返回
   ⇒ ⇒ 🔑 而分不开的时候,【一律当作还活着】
```

📌 **这个默认方向必须写死,不能靠实现者当场判断:**
```
漏回收 ⇒ 浪费,而【下一轮核对会把它捡回来】
误杀   ⇒ 把正在干活的 actor 杀了,而【捡不回来】
⇒ 两种错的代价不对称,所以默认必须偏向"不回收"
```
⚠️ 没有第二句,**任何一次读失败都会静默地变成一次回收** ——
而它发作的时机恰恰是存储最不稳的时候,也就是最可能一次回收掉一批的时候。

🔑 而这个形状今天出现了**两次**,值得单独记住:
```
· 早上:拿"ps 里没有这个连接器"当"它没起来" —— 而那分不清【真的没有】和【我这次没看见】
· 这里:拿"查不到这条 routine"当"它被删了"
⇒ ⇒ **空的结果回答不了关于结构的问题。**
```

### C.2 ⚠️ 触发点:是"死掉之后仍为零",不是"当前为零"

```
从来没有过 assign  ⇒ 表里【没有行】 ⇒ ⛔ 不回收(待命),但要可统计
曾经有、现在全死了 ⇒ 表里有行且全部 released ⇒ ✅ 回收
```
📌 这就是 §B.1 保留已释放行的全部理由。**这两种情况在"当前活跃数=0"上完全一样。**

### C.3 硬约束②③ 的回应

```
② hook:本机制【不使用任何 hook】。
   若将来要加通知/打点 hook,写死一句:【回收不经过它】,hook 失败不影响回收
③ actor 只能协作:worker 主动上报"我完事了"可以做(它能让释放更快),
   但硬上界是 §C.1 的核对 —— 它只读 run 状态,【不需要 actor 配合】
   🔑 理由:最需要回收的时刻,恰恰是这个 actor 已经不可信的时刻
```

### C.4 ⚠️ 而 §C.1 有一个前置依赖会让它整体失效 —— 见 §E1

`kind=workflow` 那一支建立在"run 一定会到终态"上。**今天不成立**(§E1 已核实)。

---

## §D 生命周期怎么算(纯函数,输入 = assign 表 + 运行时事实)

```python
def lifecycle(actor, rows, run_states, routine_states, runtime_alive) -> str:
    live = [r for r in rows if r.released_at_ms is None and _source_alive(r, ...)]
    if not rows:            return "created"     # 有主但从未派事 = 待命
    if live:                return "assigned"
    if runtime_alive:       return "releasing"   # 该释放,进程还在
    return "destroyed"
```

| 状态 | 判定式 | 含义 |
|---|---|---|
| `created` | 表里**无行** | 待命:有人对它负责,还没派事。⛔ 不回收,但要可统计 |
| `assigned` | 存在活着的 assign | 正常在役 |
| `releasing` | 有行、无活 assign、运行时仍在 | 已判定该释放,回收尚未完成 |
| `destroyed` | 有行、无活 assign、运行时已不在 | 终态 |

⚠️ **两根轴不混**(任务书 §1):
```
生命周期轴:created / assigned / releasing / destroyed  ← 只由 assign 关系决定
运行时轴  :在线 / 离线 / 正在跑 turn                    ← 只由运行时事实决定
⇒ `releasing` 是唯一一个【同时读两根轴】的状态,而它读运行时只是为了回答"清完了吗"
⛔ 不引入 `pending` —— 它同时落在两根轴上,所以它是个坏名字(任务书原话)
```
📌 全部**不落库**:落库 = 两个事实来源,而它们会分叉。

---

## §E 实施顺序(每步的可检验产出;每步都要求两头变异)

### E1 ⛔ 前置:让 run 一定会到终态 —— **已核实属实,且比任务书说的更窄也更糟**

**核实结果(任务书给的行号 622 对得上,结论要修正两处):**

```
src/hyprial/workflow/executor.py:629  （dev,_maybe_finish 内)
    except WorkflowDispatchError:
        self._report_next_attempt_ms = now + REPORT_RETRY_BACKOFF_MS   # 51: 30_000
        return
```
```
✅ 属实:report 投递失败 ⇒ run 停在 RUNNING,重试【无上限】
⇒ 修正①:不是"每 tick 重试" —— 有 30 秒退避。准确说法是
         【无次数上限、30 秒一次、且 report 阶段不被任何 deadline 覆盖】
         （deadline 只挂在 target 上:executor.py:358 / 376 / 427 / 538)
⇒ 🔑 修正②(比任务书更糟):target 派发路径【会】区分永久失败
         executor.py:553  `and not _is_permanent_failure(runtime)`
       而 report 路径【不区分】—— `except WorkflowDispatchError:` 不看 .permanent
       ⇒ 而 WorkflowDispatchError 的 docstring(executor.py:69)明写
         "an unwired user-delivery transport" 就是永久失败的例子
       ⇒ ⇒ 【一个永远送不到的 report,会让 run 永远 RUNNING】
✅ 且没有任何自动路径会终止它:cancel 只有操作员入口
   （executor.py:362 / service.py:374 / application.py:2785 的 IPC handler)
   ⇒ 查了 daemon 的 workflow tick,【没有】按年龄或 deadline 清扫 run 的扫描
```
⛔ **更正(2026-08-28,实施 E1 时查出来的):上面那个行号是 executor 的同步路径,
而 daemon 【不走它】。**
```
daemon 里 WorkflowExecutor 只在三处构造(registry.py:390 / 513 / 1076)
⇒ 三处的 dispatch 全部是 _DeferredDispatch,而它的 send(registry.py:157-185)
   只 append 一个 effect 然后正常返回 —— 【它永远不抛】
⇒ ⇒ 所以 _maybe_finish 里那句 except 在 daemon 里【不可达】
真正的生产路径:registry.py:1043-1048
   report 投递失败(异步事件)⇒ 把 run 打回 RUNNING ⇒ 排一次 30s 重试
   而 _report_retry_at 全文只有 set / pop ⇒ 【没有计数、没有上限、也不看 .permanent】
```
⚠️ 而本稿原先写的「它是 workflow.tick 楔死 daemon 的上游原因」——
**前半句(缺陷存在)对,后半句我没有验证就写了。** 生产里的原因在 registry。

📌 而这一格的教训值得单独记:
**一个被完整验证过的修复(能复现、有变异、两头都红对了),仍然可能修错了地方** ——
变异只能证明"这段代码归这些测试管",证明不了"这段代码归生产管"。
⇒ ⇒ 所以「可达性」是一个独立于测试的检查项:**它是从生产的组装点走下来的,不是从测试走上去的。**

⇒ **可检验产出**:report 阶段有上界(永久失败立即终态;非永久失败有次数或时间上限),
**而且这个上界必须在 `registry.py` 的那条路径上成立** —— 只修 executor 等于没修。
```
两头变异:
  ✅ 永久失败 ⇒ run 到终态(不再 RUNNING)
  ✅ 而【瞬时失败仍然重试并最终成功】—— 否则修法退化成"一失败就放弃"
```

### E2 assign 表 + 写入点(只写,不回收)

```
可检验产出:派发发生时写一行;run/routine 到终态时写 released_at_ms
两头变异:
  ✅ 派了 ⇒ 有活行
  ✅ 没派 ⇒ 【没有行】(而不是有一行 released) —— 区分"待命"与"做完了"
```

### E3 生命周期只读面(算出来,不落库)

```
可检验产出:一个纯函数 + 一条查询;四个状态各有一个能构造出来的样例
两头变异:
  ✅ 有活 assign ⇒ assigned
  ✅ 从未 assign ⇒ created,【不是】releasing —— 这是 §C.2 最容易写错的一格
```

### E4 周期性核对(只算、只报,**不回收**)

```
可检验产出:一个"本轮该释放 N 个"的数,以及它们是谁
📌 先报不先动 —— 与 HQ 笔记 §8.3 同一条纪律:
   【先让它可见;等到"它不为零"变成一件反常的事,再让它动手】
⚠️ 而它同时是回收逻辑的【一次免费预演】:上线前就能看出它会不会误杀
两头变异:
  ✅ 造一个该释放的 ⇒ 出现在名单里
  ✅ 造一个待命的 + 一个在役的 ⇒ 【都不在名单里】
```

### E5 回收(核对里真的动手)

```
可检验产出:该释放的被 down + destroy;名字与会话释放
两头变异:
  ✅ run 被 cancel 的 worker ⇒ 被回收(这是 45 小时事故的正面用例)
  ✅ 常驻 routine 上的 agent + 从未 assign 的待命 actor ⇒ 【一个都没动】
⛔ 这一步之前必须先有 E4 的数,并且观察过一段时间
```

### E6 CLI / 可观测(§F)

```
可检验产出:ps / top 能看出"有主无事"与"该释放"
两头变异:✅ 该显示的显示了 · ✅ 不该被标记的没有被标记
```

---

## §F 影响面

| 命令 | 现在 | 之后 |
|---|---|---|
| `hyprial agent create` (cli.py:3272) | 建名字,无归属 | 可**当场声明** assign(可选);不声明 = `created` 待命,合法 |
| `hyprial agent destroy` (cli.py:3330) | 人工 | 保留;回收路径**复用**它,不另造一条销毁语义 |
| `hyprial up` / `hyprial agent stop` | 运行时轴 | **不变** —— 它们是运行时,不是生命周期(§D 两根轴) |
| `hyprial ps` (cli.py:1866) | 在线/离线 | 增一列生命周期状态;⚠️ 与运行时状态**分列**,不合并成一个词 |
| `hyprial agent top` (cli.py:2278) | 运行时 | 增"有主无事"计数(§7.4 那个"正面画像") |

⚠️ **`agent create` 不强制要求 assign** —— 直接采纳 HQ §7.4:强制会催生垃圾 routine,
而"待命"是一个有意义的状态。**它不需要被禁止,只需要被看见。**

---

## §G 悬空问题 —— **Allen 2026-08-28 已裁,原文见各条**

> 送达路径:飞书待办 → lark-flycheck 巡检 → squire 转达。
> 两条待办 guid:`35c34941-…`(G1)、`cb060e59-…`(G2/G3/G4)。

**⇒ 裁定汇总**

| 条目 | 裁定 | 依据 |
|---|---|---|
| G1 | **B 案** | Allen 原文见下 |
| G2 / G3 / G4 | **按本稿倾向实施**(A / B / B) | 「可以,按你的建议实施」 |
| §0.1 的读法 | **未被显式裁定,但被 G1 的裁定预设** | 见 §0.1 补记 |



### G1 ⭐ 混合 assign 的 actor 是 agent 还是 worker?(最重要的一条)

```
现象:任务书的分类规则是关于【一条 assign】的,而 assign 是多对多
     ⇒ 同时 assign 到 1 条 routine + 3 个一次性 workflow 的 actor,规则不判
     ⚠️ 而 h2b-developer 今天正是这个 actor —— 机制的动机来源落在规则的空隙里
A 案:只要存在【任一条 routine assign】⇒ agent
     优点:与回收规则天然一致(routine 活着 ⇒ 不回收);实现就是一个 EXISTS
     缺点:一条随手加的 routine assign 会把 worker 永久变成 agent
B 案:agent/worker 【不是 actor 的属性,是问句的答案】——
     不存字段、不显示单一标签,只显示"它的 assign 里有哪些 kind"
     优点:不需要在二义处硬造一个答案;也不需要 Allen 现在就裁
     缺点:`ps` 上没有一个能一眼扫的词
⇒ 🔑 我倾向 B。理由:这个二义【不是命名问题,是模型里真实存在的一格】——
   而 A 案是在用一条规则把它盖住。而今晚的教训是:盖住的东西会在一年后原样长回来。
⚠️ 但 B 有一个我承认的代价:Allen 最初的问句就是"要不要区分 worker 和 agent",
   而 B 的回答是"这个区分不该是一个字段"。这需要他本人点头,我不替他答。
```

✅ **Allen 2026-08-28 裁定:B。原文:**
> 「按B处理,没有必要在字段区分agent还是worker,通过workflow/routine来管理就可以,
>  agent/worker的区分实际上不需要体现在代码中,而是体现在**任务的分配上**,
>  agent/worker 是我们在**设计时使用的概念**」

📌 ⇒ 落到本稿的具体后果(三条,写死):
```
① ⛔ 不新增任何 agent/worker 字段、不在 ps 上显示单一标签
② `assign_kind` 仍然保留 —— 它不是"这个 actor 是什么",是【这条 assign 指向什么】
   🔑 这两者的区别正是裁定的内容:分类属于【分配】,不属于【实体】
③ 文档里 agent/worker 继续用(它们是设计词汇),但**代码里不出现**
   ⇒ 守卫:若将来有人加 `is_agent` / `actor_type` 之类的列,应当被这条裁定挡回
```

### G2 「routine 退役」是不是一个已有操作?

```
现状:routines 表有 enabled 列,但【未查】 disable 与"退役"是否同义
     —— 若 disable 只是暂停,那 assign 不该因它而死
A 案:新增显式 `routine retire`(不可逆),与 disable 分开
B 案:复用 enabled=0
⇒ 倾向 A。理由:HQ §6.3 —— routine 的终止是一个【决定】不是一个【事件】,
   而"暂停"与"退役"混用会让一次误操作释放掉一批 agent
```

### G3 owner 链(HQ §8.3)进不进这一版?

```
A 案:进 —— 一起把责任归属做了
B 案:不进 —— 这一版只做 assign;owner 链单独一格
⇒ 倾向 B。理由:任务书说的是"单一名词 assign",而 owner 是另一条边;
  一次做两条边会让 §C 的不变式同时依赖两个未落地的概念
⚠️ 但 HQ §8.3 那个"可见的数"(owner 链没落到人的 agent 有几个)很便宜,
  可以在 E4 的报数里【顺带】出一格 —— 若 Allen 要,我加
```

### G4 assign 的写入点由谁负责?

```
现状:派发发生在多处(routine service / workflow executor / 手工 CLI)
A 案:每个派发方各自写 assign 表
     ⚠️ 那就是"声明了没检查"的漂移形状 —— 少写一处就少一条边,而【没有东西会报错】
B 案:收敛到一个写入点(dispatch 层),所有派发都经过它
⇒ 倾向 B,而且这可能是**整个机制成败的那一格**:
  🔑 assign 表【漏写】比【写错】危险得多 —— 漏写 ⇒ 该表看起来一切正常,
     而那个 actor 会被当成"从未 assign"永远不回收
⚠️ 而 B 的前置是"是否真的存在一个所有派发都经过的单点" —— 【未查】
```

---

## §G5 ⇒ 回应 Allen 的反问:**「workflow 与 routine 的本质区别只是有没有环,对吗?」**

**答:环是一个真实且有用的看法,但它不是这条机制需要的那个区别 —— 而且两者互相独立。**
⇒ 而"独立"不是推理,我们自己的代码里**两个方向各有一个现成的反例**:

```
① 有环、但会终止:PAC 的重试就是一个环(dispatch → timeout → 再 dispatch)
   executor.py:554  `runtime.attempts < policy.max_attempts`   ⇒ 有界 ⇒ 终止
② 无环、但不终止:昨夜刚核出的那条缺陷(§E1)
   所有 target 都已终态、图上没有任何环,而 report 投递永久失败
   ⇒ run 永远停在 RUNNING
⇒ ⇒ 🔑 有环 ⇏ 不终止;无环 ⇏ 终止。**这是两条互相独立的性质。**
```

📌 **而 assign 需要的是第二条,不是第一条**:
```
回收的不变式(§C.1)问的是:【这条 assign 的来源死了没有】
⇒ 而"死了没有"要能被一次周期性核对【读出来】
   workflow ⇒ 有可观测终态(COMPLETED / CANCELLED)⇒ 读得出来
   routine  ⇒ 【没有】终态,它结束是因为有人【决定】退役它(HQ §6.3)⇒ 读不出来,要有人做
⇒ ⇒ 所以两者必须在【怎么判断活没活】这一层保持可分,而不在【图的形状】那一层
```

✅ **而你的直觉里对的那一半,本稿已经采纳了**:在**数据层它们已经统一** ——
```
一张 actor_assign 表,assign_kind 只是一个值('routine' | 'workflow')
⇒ 也就是说:assign 这条边【不关心】对面是什么形状的东西
⇒ 唯一需要分开的是 §C.1 里那一行"来源是否仍然活着"的求值方式
```

⛔ **而如果真的把两者合并成"一个可能带环的 workflow",会付两笔代价:**
```
① 直接违反 #235 §0 判断二:「PAC v1 的 run 有确定性终局义务,长驻 run 直接违反」
   ⇒ 那条判断正是为了让失败域保持可枚举 —— 合并会把它退回不可枚举
② 回收失去立足点:一个不终止的 run,"它死了没有"这个问题【没有答案】
   ⇒ 而那正是 45 小时事故的形状:活得比任务长,而没有东西认为这有问题
```

⇒ 📌 **我的建议:概念上"routine = 会重复的 workflow"可以留作说法,但机制上保持两个**
   —— 因为区分它们的不是形状,是**有没有一个机器读得出的终点**。
⚠️ 而这一条我**只从 assign 的角度回答**;若你问的是更大范围的合并(调度、CLI、schema 是否也合并),
   那超出本稿,我**没有查**,不假装有答案。

## §G6 ✅ 顺带核实:「workflow/routine 应该也有一个 sqlite 文件记录」

**两边都已经有了,不需要新增工作:**
```
workflow/store.py:111  CREATE TABLE runs (…)      —— 另有 targets / effects /
                       workflow_external_refs / agent_task_* 共 9 张表
routine/store.py:20    CREATE TABLE routines (…)  —— 另有 in_flight / routine_cycles
```
⇒ 所以 §C.1 里"求值来源是否活着"**读的是已经落库的事实**,不需要先补持久化。

---

## §H 与既有材料的关系

```
✅ 继承 #235:routine 作为常驻载体、PAC run 作为一次性载体 —— 本稿不改动那一层
✅ 继承 HQ 笔记 §6.1(退役 = 释放事件)、§7.4(空归属是有意义的状态,不禁止只可见)、
       §8.3(先报不先堵)
⛔ 取代 HQ 笔记 §7.3 / §8.4 的四关系模型(owner / mandate / assignment / binding)
   ⇒ 被 Allen 2026-08-28 的"单一名词 assign"取代 ⇒ 合并为 owner / assign / binding
   ⚠️ 而合并的代价写在 §0.1,不是无声的
📌 #235 的 §8 / §9(task-association / depend)与本稿正交,未触碰
```

## §I 未查清单(集中列出,便于下一个人接)

```
· 跨机器 assign(actor 在别的节点)
· transfer 路径与 assign 的交互
· routine 的 disable 与"退役"是否同义(G2)
· 是否存在所有派发都经过的单一 dispatch 点(G4 —— B 案的前置)
· MCP / harness 侧有无平行的归属概念
```
