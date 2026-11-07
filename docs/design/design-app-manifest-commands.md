# App manifest 扩展:命令挂载(H3)+ 事件订阅(H1/H2)

> **已退役（2026-10-03，Allen 裁决；2026-10-13 命令面更新）：代码已删除，现行占位
> `hyprial socialware install` 返回 `COMING_SOON`；旧一级 `hyprial install` 已删除。**
> 命令挂载（`discover_mounts` / `_mount_app_commands`）随整套应用安装体系一并删除；
> `hyprial gui` 改为内建命令，直接使用 wheel 内的 GUI 捆绑物。本文正文保留为历史记录，
> 不再描述现行行为。

**状态:** 设计稿,待 devusr(`agent:amyws:devusr-dev:cc-kanban`)对齐字段后实现。
**依据:** `hq/notes/intent-kanban-socialware-2026-09-04.md` §3.4 H1/H2/H3、§7 Allen 裁定。
**范围:** 本稿只定 **manifest 的数据形状** 与 **挂载语义**;H1 的事件表实现归 devusr。

⚠️ 三格共用同一份 manifest 扩展,所以字段**一起定**。只按 H3 设计,H2 落地时必然要改
schema —— 那是两边各写一半、合并时才发现语义不同的经典返工点。

---

## 0. ⭐ 先更正 spec 的一处「已核」——它让 H3 看起来比实际大

§3.4 写:「没有 `hyprial kanban` 这种「起某个 app」的入口(只有 `hyprial gui`)」。

**这句话对的是【CLI 表面】,不对的是【机制】。** 实测(`origin/dev` 7d9aa1c):

```
installers/core.py:56   Manifest(name, install, start)          ← start 已在 manifest 里
installers/core.py:654  launch_application(name, hyprial_home=...)  ← ⭐ 已经是按 name 参数化的
cli.py:1983             return launch_application("gui", ...)   ← 唯一写死的是【这个字面量】
```

⇒ **起 app 的机制已经通用了;写死的只有 CLI 子命令的注册。**
⇒ 所以 H3 不是「造一套挂载机制」,而是三件小得多的事:
① manifest 声明命令 ② 启动时按已装 manifest **动态注册**子命令 ③ 把 `gui` 变成其中一个。

📌 判据:**「入口不存在」与「入口没有被注册出来」长得一样,而修法完全不同。**
读 CLI 表面会得到第一个结论;读 `launch_application` 的签名才看得到第二个。

---

## 1. manifest 扩展(schema 从 `hyprial.install/v1` → `hyprial.install/v2`)

```jsonc
{
  "schema": "hyprial.install/v2",
  "name": "kanban",
  "install": ["bash", "install.sh"],
  "start":   ["bash", "start.sh"],

  // ── H3:本 app 提供哪些 hyprial 子命令 ────────────────────────────
  "commands": [
    {
      "name": "kanban",              // ⇒ hyprial kanban
      "summary": "起看板 GUI + 本 owner 的 kanban agent",
      "actions": ["start", "status", "stop"]   // 省略 ⇒ 只有 start
    }
  ],

  // ── H2:本 app 要订阅哪些 hyprial 事件、由哪个 actor 接 ──────────
  "subscriptions": [
    { "event": "pac.run.dispatched", "actor": "kanban" },
    { "event": "pac.run.reply",      "actor": "kanban" },
    { "event": "pac.run.terminal",   "actor": "kanban" },
    { "event": "pac.run.timeout",    "actor": "kanban" }
  ]
}
```

**字段约束(全部 fail-loud,⛔ 不静默忽略未知字段):**

| 字段 | 规则 | 为什么 |
|---|---|---|
| `commands[].name` | `^[a-z][a-z0-9-]*$`,且⛔**不得与内置命令重名** | 见 §2 冲突 |
| `commands[].actions` | 子集 of `start\|status\|stop\|upgrade` | 动作是闭集,不是自由字符串 |
| `subscriptions[].event` | 必须在 daemon 已知事件表里 | ⛔ 订阅一个不存在的事件必须当场红,否则是永远收不到消息的静默失败 |
| `subscriptions[].actor` | 短名,daemon 解析成当前 owner 的 URI | ⭐ 见 §4 |
| 未知顶层键 | **报错** | v1→v2 是换代,不是宽松化 |

⚠️ **`actor` 存短名,⛔ 不存派生 URI。** 这是 UIDFIX 那轮的现成教训:
MCP 把 `--actor` 写死成派生 URI,owner 归一后每次调用被回 `SESSION_SUPERSEDED`,
两个会话入站全断(卡 `d78ccc6f`)。⇒ **任何持久化的东西存短名,URI 在用的时候现算。**

---

## 2. 命令冲突:三类,处置不同

```
① 与内置命令重名(kanban vs ps/send/init…)  ⇒ ⛔ 安装时拒绝,报出冲突的那个名字
② 两个 app 声明同一个命令名                  ⇒ ⛔ 后装的那个安装失败,报出先占者
③ 与【未来】新增的内置命令重名                ⇒ ⚠️ 内置优先,启动时 warn 一次并跳过挂载
```
⇒ ③ 不能也用「拒绝」:那会让 hyprial 自己加一个新内置命令时,把用户已装的 app 变成装不上。
⇒ **测试:三类各一条;并变异——去掉内置名单检查 ⇒ ① 必须红。**

---

## 3. 动态注册怎么落(H3 的实现要点)

```
启动时读【已装 app 的 manifest】⇒ 为每个 commands[] 注册一个 typer 子命令
⇒ 该子命令的实现是通用的:  hyprial <cmd> [start|status|stop] → launch_application(<app>, ...)
```
⚠️ **代价要说清:这让 `hyprial --help` 依赖磁盘状态。** 于是:
- 读 manifest 失败 ⇒ ⛔ 不能让整个 CLI 起不来;跳过那个 app + 一条 warn(⭐ 这条 warn 必须可见,
  否则「命令消失了」和「从没装过」长得一样)
- 注册要**廉价**:只读已装 manifest 的缓存索引,⛔ 不在每次 `hyprial --help` 时走 registry/网络

**`hyprial gui` 迁入 + 删写死入口:**
```
cli.py:1937 def gui(...)              ⇒ 删
cli.py:2042/2050/2058 三个 gui_* 转发 ⇒ 删
hyprial/gui_runtime.py                    ⇒ ⚠️ 由「app 隐含」改成「app 参数化」
                                        (今天 gui_status/stop_gui/start_gui_background
                                         只吃 hyprial_home,app 是隐含的)
```
⛔ 按 Allen 裁定**不保留** `hyprial gui` 的向后兼容入口,迁完即删。

---

## 4. ⛔ 我发现一处与既有裁定冲突的地方,需要 Allen 或 hq-adjutant 定

§3.4 H3 末句:「都归 **daemon 托管(重启后恢复)**」。

**而今天的 app 进程不是 daemon 托管的**(实测 `gui_runtime.py`):
```
subprocess.Popen(..., start_new_session=True)  + 一份 app 目录下的文件记录(pid + identity)
⇒ 没有 desired-state 条目,没有 daemon 监管,daemon 重启【不会】恢复它
```
⚠️ 而「让 daemon 托管子进程并跨重启恢复」正是 **U0c 明确判为超出射程、并归了卡的东西**
(`spec-pr332-fixes-2026-09-03.md`):

> daemon 对 harness 子进程无持久 pid 登记表(launcher 均 `start_new_session`、无 pidfile),
> 跨代杀孤儿需要新建 pid 登记 + 收割子系统 ⇒ ⛔ 不搭在补偿单上;归卡 `9bec2250` / `9a00e430`。

⇒ **H3 若真要「daemon 托管 + 重启后恢复」,它依赖的正是那个被推迟并归卡的子系统。**
⇒ 三个选项,我建议 **B**:
```
A 本单一并做 pid 登记 + 收割       ⇒ 射程暴涨,且与 9bec2250/9a00e430 重复建设
B ⭐ 本单只做【挂载 + 启停】,「重启后恢复」显式留给 9bec2250 落地后再接
   ⇒ 本单交付物写明:hyprial <cmd> start/stop 可用;daemon 重启后【不自动恢复】,已知限制
C 用 desired-state 记一条「期望运行」,恢复由现有 restore 路径做
   ⇒ ⚠️ 需先答:app 进程与 harness 连接器是不是同一种被管对象?
     若不是,会造出第二套 owner —— 而那正是 U0BRACE 刚消灭的形态
```
⛔ 我不自己选。**这一格没定之前,我不写「恢复」相关的任何代码。**

---

## 4b. ⭐ 解除对 devusr 的等待:`subscriptions[]` 做成【保留槽】(2026-09-04 更新)

`cc-kanban` 回复:他那单 **HOLD 等 amyws 本人裁**,五条字段**一条都确认不了**,
并明确要我别等:「你正在等一个可能不会来的信号」。

⇒ 于是本稿改成:**`commands[]` 现在就做;`subscriptions[]` 只保留槽位、非空即报错。**
⚠️ 这不是我发明的折中,是**本仓已有的先例**,我核过原文:

```python
# src/hyprial/workflow/schema.py:337-345   (PAC v1 的 hooks)
# ── hooks (v2 extension slot; v1 accepts only an empty mapping) ───────
hooks_raw = root.get("hooks")
if hooks_raw is not None:
    hooks = _require_mapping(hooks_raw, f"{label}.hooks")
    if hooks:
        raise WorkflowSchemaError(
            f"{label}.hooks names {sorted(hooks)} but v1 ships no named hooks "
            "— this section is the v2 extension slot (design-pac-workflow §4.7)"
        )
```

⇒ `subscriptions[]` 照抄这个形状:
```
键可以出现,但必须是空的;非空 ⇒ 报错,且报错文本要【点名 H2 与本文档】,
形如:"manifest.subscriptions names [...] but this release ships no event
       subscriptions — this is the H2 slot (design-app-manifest-commands §1)"
```
⭐ **为什么这比「先按我猜的字段实现」好:** 一个**会报错的空槽**,让「H2 还没做」
变成一件**当场可见**的事;而一个我猜着填好的 schema,会让 devusr 上线后面对一份
**看起来已经定了**的字段表 —— 那时改它要付「推翻既成事实」的代价,而不只是设计代价。
⇒ ⛔ 保留槽不需要任何人确认字段;它恰恰是【承认字段还没定】的那个形状。

📌 而 ③(actor 存短名)**已经不需要等** —— cc-kanban 给了第二条独立证据:
```
真板 42 个不同的 owner 取值,只有 2 个能解回当时有效的 agent URI ⇒ 落盘 URI 失效率 40/42
kanban 闸门的现行实现:HYPRIAL_PRINCIPAL 存人的短名 / HYPRIAL_AGENT 存 agent 短名,一个 URI 都不落盘
```
⇒ 与我这边的证据(MCP `--actor` 写死派生 URI ⇒ owner 归一后全断,卡 `d78ccc6f`)
**是两条独立路径指向同一形状**,不是互相转述。⇒ ③ 定为规则,不再是待确认项。

## 5. 与 devusr 的接口面(⏸ 等他放行后再对,⛔ 不阻塞本单)

```
① schema 版本号一起跳到 hyprial.install/v2(⛔ 不要各跳各的)
② subscriptions[] 的字段名与取值域(event 的命名空间:pac.run.* 由 H1 定)
③ actor 存短名这一条(见 §1 的 ⚠️)
④ 订阅表是 daemon 持久状态(spec 已定),清理时机:hyprial agent destroy 该 actor ⇒ 清
⑤ ⛔ 各自不改对方那段:commands[] 归我,subscriptions[] 归 devusr;
   共用的只有「顶层 schema 版本 + 未知键报错」这一条规则
```

## 6. 测试(每条修法配一条)

```
· manifest v2 解析:commands/subscriptions 各自的合法与非法形态
· 命令冲突三类各一条 + 变异(去掉内置名单检查 ⇒ ① 红)
· 动态注册:装了 app ⇒ hyprial <cmd> 出现在 --help;卸载 ⇒ 消失
· ⭐ manifest 坏掉 ⇒ CLI 仍能起,且 warn 可见(⛔ 不允许静默跳过)
· gui 迁入后:hyprial gui 不再存在(写死入口已删),hyprial <gui-app-cmd> 走通用路径
```


## §6 切换:旧二进制读不了 v2 —— `hyprial install <app>` 对已装 app = 升级(2026-09-04 夜)

⛔ 本稿此前写的切换顺序(先合 gui 的 v2 manifest,再各节点 `hyprial install gui --yes`
刷新,再合本 PR)**在根上错了**,而且已经照它走了一步(dsh-hyprial-talk #87 已合)。
错在一个没跑过的检查:**旧二进制能不能把那份 receipt 写出来。**

实测(装机 hyprial = dev 7d9aa1c,对 dsh main 746e74be 的 manifest):
```
core.py _read_manifest       if raw.get("schema") != _SCHEMA: raise …     (_SCHEMA = hyprial.install/v1)
hyprial install gui --yes        INSTALL_UPDATE_UNSUPPORTED  已装拒绝原地动(MVP 限制)
hyprial gui upgrade --yes        INSTALL_MANIFEST_INVALID    staged checkout 的 v2 manifest 过不了 v1 相等检查
hyprial gui upgrade --check      绿 —— check 不打开 manifest,所以它答不了这个问题
```
⇒ 旧二进制**既装不了也升不了** v2 的 gui。而本 PR 之后 `hyprial gui upgrade` 是【挂载】命令,
只在本机 manifest 已经声明它时才存在 —— 正是升级要去拿的那份。先有鸡:没有任何命令能把
pre-v2 的安装往前挪。

### 修法:`install` 已装即升级

```
hyprial install <app> [--yes] [--check] [--force]
  无 receipt  ⇒ 全新安装(不变)
  有 receipt  ⇒ upgrade_application:同一套 plan / confirm / dirty / diverged 判定,
                app 在跑则 停 → 升 → 重启(复用挂载命令 upgrade 的那段)。结果带 mode=upgrade
  --check / --force 只对已装 app 有意义;对未装 app 报 INVALID_ARGUMENT
```
MVP 的 `INSTALL_UPDATE_UNSUPPORTED` 退场。它守的是「install 不悄悄改已装内容」——
现在这一格由 upgrade 的 plan+confirm 守(`--yes` 才动,dirty/diverged 无 `--force` 拒绝并**原样保留**:
receipt、checkout HEAD、manifest、本地改动四项均有断言,`tests/test_cli_install.py`)。

⛔ 不做 `hyprial upgrade <app>`:`hyprial upgrade` 在一级命令面裁决稿(#292,cli-target-command-surface.md :45)
已经是 hyprial 自身的升级,不是空位。

### 修后的切换顺序(#87 先后不再是约束)

```
① 本 PR 进 dev
② 各节点升级 hyprial
③ 有 apps/<app>/install.json 的节点:hyprial install gui --yes   ← 这时才有效;没装过的节点不做
④ hyprial gui status 出现(或 hyprial --help 列出 gui)⇒ 挂载成功
```
过渡期副作用(#87 合入 → 各节点走完 ②③):v1 二进制上 gui 的 install/upgrade 不可用。
已核:仓内没有自动化调 `hyprial install gui`(hq tools/、PAC 模板、crontab、LaunchAgents、squire setup 均 0 命中),
只影响人手敲的人。

📌 记一笔方法:上一版顺序的每一步都对着「挂载读什么」推,没有一步对着「谁来写它」推。
**一个只在 A 版本能读的文件,要问 B 版本写不写得出来** —— 那是一个可以当场跑的检查,当时没跑。
