# 深模块重构 C0 扫描与脚本任务规格

- 日期：2026-10-01
- 产品基线：`613893e4b9016f80aebe52532595101782437d02`。
- 本轮范围：完成只读 C0 扫描、工具验证和后续完整脚本的任务规格；不执行产品重构。
- 后续工作方式：主模型确定边界和映射，快模型先编写完整脚本套件，主模型审阅、修改，再在隔离 checkout 分阶段执行和验证。
- 原方案：[`refactor-plan-deep-modules-2026-10-01.md`](refactor-plan-deep-modules-2026-10-01.md)（同目录）。C0 保留其原始映射作为比较基线，不把原映射当作已批准的新架构。

## 1. C0 要回答的问题

1. 真实依赖有哪些？将包、顶层文件、相对 import、函数内 import、类型检查 import 一并映射，分别报告 import 站点、导入符号和图边。
2. 每条逆向依赖需要什么处理？区分纯词汇表、实现职责、业务装配、CLI 展示和平台资源；不能用模块归并或动态 import 冒充解耦。
3. 公共面需要暴露哪些名字？从真实消费方收集候选，不自动重导出全部实现；检查同名冲突和现有 authority 约束。
4. 哪些运行时入口会断？包括 `python -m`、importlib、包资源、生成配置中的命令、模块相对路径、`__file__` 父目录计算。
5. 哪些消费者必须同步改？包括 tests、scripts、contract、desktop、GUI 的 Python 集成、CI、构建文件和运行时 Markdown 输入。
6. CLI/application 如何拆？记录符号身份、注册顺序、方法调用和字段读写，给出状态与生命周期所有权，再编写抽取规则。
7. 使用哪个工具可保留源码？对仓库全部受版本控制 Python 文件执行解析验证，对 LibCST 检查逐字节无操作 round-trip。

## 2. 扫描范围与产物

扫描不 import 产品模块，不访问用户 home、daemon、真实 provider 或服务管理器。输入通过 `git ls-files -z` 枚举；记录 HEAD、工作区状态、映射和输入哈希。报告中的发现只是一项静态证据，不自动变成替换规则。

| 产物 | 内容 | 审阅用途 |
|---|---|---|
| `inventory.json` | 受版本控制输入、scope、字节数、SHA-256、源码模块身份 | 防遗漏、发现基线漂移 |
| `imports.json` | 已解析 import、原路径/行号、符号、alias、相对层数、local/TYPE_CHECKING 信息 | 建立完整消费者清单 |
| `boundaries.json` | 原映射的方向违规和 SCC；站点数与边数分别统计 | 重新估计解耦范围 |
| `references.json` | 模块字符串、资源、argv 入口、路径计算和跨语言路径引用 | 建立非 import 迁移规则 |
| `exports.json` | 现有 `__all__`、reexports、定义签名、ports 的消费者 | 起草公共 API manifest |
| `exemptions.json` | 三个现有 manifest 的完整身份和理由 | 搬迁前后保持 authority 约束 |
| `extraction.json` | CLI 注册调用及位置；application 直接方法、字段读写、自调用 | 起草符号抽取与依赖注入规则，不代替所有权审定 |
| `coverage.json` | 扫描/跳过/语法错误/未解析引用/未映射源码、分析盲点 | 明确扫描能证明什么 |
| `summary.md` | 主要统计、原方案差异、待决事项、证据路径 | C0 审阅入口 |
| `parser-probe.json` | Python 3.12 AST、LibCST、Bowler/Fissix 的解析结果；LibCST 字节一致性 | 工具实测选择 |

历史 `notes/` 和复现材料单独标记。语法失败及历史旧路径如实列出，但不能因其存在就修改证据。使用有限的 import/路径证据，避免将任意字符串或配置内容复制到报告。

### C0 完成条件

- 354 个产品 Python 文件全部计入；新增/删除文件必须显式说明。
- 产品源码无解析失败、无未映射文件。未解析引用和动态引用全部可定位，不能静默丢弃。
- 绝对、相对、alias、local、TYPE_CHECKING 和 from-package 子模块导入有针对性测试。
- 可重现原方案映射的依赖统计；与上轮 74 站点的差异必须通过证据解释，不硬编码数量。
- 扫描前后输入哈希相同；输出不覆盖源文件；稳定输入生成稳定报告。
- 现有 3/66/60 条豁免完整保留；authority 的路径变化与行为变化分开。
- LibCST 在全部活跃、计划改写的 Python 输入上解析成功并保持无操作字节一致。

C0 的扫描完成不等于重构可执行。后者还要求每个待修改引用、公共导出、符号抽取及运行时配置迁移均有主模型审阅过的明确规则。

## 3. 工具分工

| 工作 | 工具 | 使用约束 |
|---|---|---|
| 输入、基线、差异 | Git、`rg`、SHA-256 | 枚举 tracked 输入；搜索只产生候选，不全仓 replace |
| 依赖和状态扫描 | Python 3.12 标准库 AST、tokenize | 只读；不 import 产品；保留源码位置和作用域 |
| Python 重写 | LibCST `1.9.0`，C0 实测通过 | 保留注释、空白、括号和编码；处理符号绑定，不只换路径文本 |
| Bowler 可行性比较 | Bowler `0.9.0` / Fissix | C0 只做解析对照；实际选择服从仓库覆盖实测 |
| JSON/YAML/TOML/JS/Markdown 路径 | 每种格式的显式位置规则 | JSON 按字段；Python 由 CST；其他格式仅对审定 span 改写并校验匹配次数 |
| 文件/资源搬迁 | 映射 manifest + 文件系统操作 + Git diff | 检查目标冲突、大小写冲突和包资源；不覆盖已有目标 |
| 验收 | 单元/authority gate、Ruff、contract、wheel 与隔离 E2E | 按阶段执行；错误中止并保留失败 checkout |

LibCST 的官方文档说明其支持保留格式的 CST 与 codemod 元数据框架；Bowler 官方说明其使用 Fissix。Python 的 lib2to3 旧语法限制是需要做实测的理由，不能仅凭标准库删除 lib2to3 就断言 Fissix 不可用。

- https://libcst.readthedocs.io/en/latest/
- https://libcst.readthedocs.io/en/latest/codemods.html
- https://pybowler.io/docs/basics-intro
- https://docs.python.org/3.12/library/2to3.html

工具依赖放入独立 uv 环境，固定直接依赖并在脚本实现阶段提交独立工具 lockfile。扫描器只依赖标准库；实际变换套件使用 Python 3.12 + LibCST 1.9.0，Bowler/Fissix 只属于可行性对照环境。C0 不修改产品 `pyproject.toml`、`uv.lock` 或用户已有 `uv.toml`。

## 4. 主模型负责的边界决策

保留五个一级模块的候选方向：`kernel ← identity ← daemon ← biz ← shell`。允许引用任意较低层，不要求相邻层。上层装配代码负责把业务实现和 handler 注入 daemon；daemon 的核心运行时不直接 import biz/shell。

| 已发现的类别 | 候选处置 | 必须审定的细节 |
|---|---|---|
| lifecycle receipts、launch spec | 提取共享契约 | 数据验证、序列化、异常类型及所需依赖 |
| counters、alarm、availability | 拆分基础观测/投递契约与业务策略 | counters 是否基础设施；alarm 数据和投递是否核心；usage/quota 策略是否 biz |
| routine/usage 装配与 IPC | 从上层注入 service、handler 和 lifecycle hook | 启动、恢复、关闭、迟到回调和 handler 所有权 |
| workflow/cli、PAC/CLI 错误、orgfs/_state_dir | 展示逻辑放 shell；基础路径/协议异常放其语义所有者 | CLI 参数/输出不变；禁止 lower-layer 依赖 CliError |
| identity/harness/profile/principal | 纯契约下沉或通过 port 注入实现 | 不把整个 PAC、harness 或 profile 实现塞入 kernel |
| home 初始化、Windows owned process | 分开基础路径/进程事实与装配/迁移 | home 迁移时机、dispatch policy 初始化、Windows 资源所有权 |

公共 API 采用审定的 `__all__` 和显式 lazy 转发/工厂，不使用 `from .interface import *` 触发所有 lazy export。允许 interface 引用本模块实现；禁止跨模块绕过公共面。门需要理解相对 import、`from package import impl`、alias 和可识别动态 import，不能仅检查 `.impl` 子串。

application 协作者的输入是字段/方法依赖清单、state owner、start/close owner 和回调规则。若暂用 host，必须列出准确的访问清单和后续收窄条件；这种过渡不能被记作深模块验收已完成。

## 5. 交给快模型的完整脚本任务规格

### 交付顺序

1. 主模型以 C0 证据审定规则，并编写 manifest；快模型不自行决定领域归属、公共 API 或移除豁免。
2. 快模型在独立 checkout 编写全部脚本、针对性测试、命令说明和计划预览；完成前不对产品应用变换。
3. 主模型审阅所有代码及累计 dry-run diff，修改规则/脚本，复核测试；对待执行的 manifest 和工具版本记录哈希。
4. 主模型在另一份隔离 checkout 执行脚本，逐阶段验收；失败后修订同一套脚本并重放，不留下仅存在于结果 checkout 的手工修补。
5. 全部验证通过后交付可审阅变更。远端 push、PR/合并等另按会话授权执行，不由脚本附带完成。

快模型的文件写入范围限定为 `scripts/refactor/` 和 `tests/refactor/`；领域映射、公共 API、抽取规则等正式 manifest 由主模型审定，快模型不能通过修改输入规则让测试变绿。工作区使用独立 clone，不把当前用户的脏工作区作为执行目标。本轮已用快模型完成扫描脚本的这一协作流程；完整重构脚本仍是后续交付，不冒充已实现。

### 脚本套件

| 文件/模块 | 必须实现 |
|---|---|
| `run.py` | `plan`、`apply`、`verify`、`status`；选择阶段；默认只预览 |
| `manifest.schema.json` / `validate_manifest.py` | 校验映射、符号身份、输入哈希、匹配次数、输出冲突和完整阶段列表 |
| `transforms/imports.py` | absolute/relative/from-package 导入、alias 和绑定使用；同一 import 的拆分 |
| `transforms/symbols.py` | 按显式旧/新符号与依赖清单抽取词汇表、CLI 家族和 application 协作者 |
| `transforms/facades.py` | 从审定 export 清单生成公共面；lazy 行为；导出冲突拒绝 |
| `transforms/references.py` | 模块入口、资源、配置键、父目录路径计算和跨语言消费方 |
| `transforms/layout.py` | 文件和资源映射、tests/scripts/docs 阶段；历史证据字节不变 |
| `verify.py` | 基线与 diff 验证、门的检测能力、接口/命令快照、构建和分阶段命令 |
| `tests/` | 变换边界、预览无写入、冲突/漂移拒绝、幂等、阶段失败、包资源与绑定语义 |

“完整脚本”意味着全部阶段都有具体变换和验证命令；`TODO`、空阶段、成功退出的占位代码或在 apply 中询问模型如何设计，都不能通过审阅。

Python import 变换必须使用 LibCST 的作用域、限定名和位置元数据，或提供等价的明确绑定分析。例如 `import hyprial.agents.registry` 后使用 `hyprial.agents.registry.Registry`，只改 import 行会让使用处断裂；局部变量遮蔽、alias、模块重导出和混合 from-import 都必须有针对性测试。涉及动态反射但无法证明绑定的引用，进入待决清单并中止相应变换。

最少测试集合包括：多层相对 import；`from package import child`；一个站点导入多个符号；旧模块中不同符号迁往不同公共面；显式 alias 和无 alias 的绑定使用；局部名称遮蔽；懒加载不因 package import 提前触发；编码/CRLF/尾部注释保留；Windows 大小写路径冲突；资源随包迁移；生成配置的旧入口迁移；已有豁免身份保持；二次执行零 diff；故障后明确阶段状态；WORM 文件哈希保持。

### Manifest 的最小记录

```json
{
  "schema_version": 1,
  "baseline_commit": "613893e4b9016f80aebe52532595101782437d02",
  "stage": "S2",
  "rule_id": "example-only-not-approved",
  "kind": "import_symbol",
  "source_path": "src/hyprial/agents/worker_state.py",
  "source_sha256": "filled-from-C0",
  "old_module": "hyprial.daemon.desired_state",
  "old_symbol": "HarnessLaunchSpec",
  "new_module": "hyprial.kernel",
  "new_symbol": "HarnessLaunchSpec",
  "expected_matches": 1,
  "reason": "shared launch contract; concrete dependency audit required"
}
```

正式 manifest 必须覆盖文件、符号、模块入口、资源及配置规则。混合导入应按 symbol 拆分；不能把 `desired_state` 的全部名字都映射到 kernel。示例只展示格式，不授权实际改写。

### 执行约束

- 输入 pin 到 commit + 文件 SHA-256；基线漂移或相关路径有未记录本地改动就拒绝执行。保留用户 `.idea/hyprial.iml`、`uv.toml` 等现有改动。
- `plan` 产生完整 moves、每条规则的匹配计数、unified diff、尚未覆盖的引用、预计影响的 gate/consumer；不修改产品输入。
- `apply` 仅用于已经完成主模型审阅的隔离 checkout；准确标记身份，不对 clone/worktree 所在父目录进行删除。
- 写入前验证所有目标和前置条件；每阶段生成输入/输出哈希、规则日志和完成记录；失败明确非零退出。
- 幂等需要两次计划/执行证明：已完成规则是明确的零修改状态，不能用静默跳过掩盖源符号丢失。
- 所有写入在输出日志中可追溯；不使用 `git reset --hard`、全仓 replace 或自动提交/推送。
- 不对 WORM 证据内容做批量替换；历史路径可保留在证据内，活动索引和链接单独修改。
- 现有豁免只迁路径和必要的 owner 身份；删除需要行为消失的代码证据及门的 mutation 测试。
- 根目录识别不能依赖搬迁后的固定 parents 深度；路径/资源规则同阶段更新消费方。

## 6. 后续执行阶段与验收

| 阶段 | 工作 | 验收重点 |
|---|---|---|
| S1 契约与解耦 | 按审定规则提取共享契约；消除 lower-layer 对 CLI/业务/上层实现的依赖；业务装配使用注入 | 基于候选映射的方向扫描；原有 authority gate；相关功能和生命周期测试 |
| S2 原子迁移 | 文件/资源搬迁、全部消费者、公共 facade、运行时入口、构建路径、现有 gate/manifest 同阶段改写 | 新模块方向及公共面 gate、安装后 import、worker 入口、资源加载、全量 pytest/Ruff/contract |
| S3 CLI 拆分 | 按明确 family/symbol 注册顺序抽取，公共入口保留 | 完整命令树、各族参数/default/隐藏标记、help 快照、JSON/退出码；不只检查顶层 inventory |
| S4 application 拆分 | 按明确状态和生命周期所有权抽取 IPC/恢复/维护协作者 | 时序、IPC 写超时、恢复/启动/关闭、迟到回调、补偿与 authority；报告 host 剩余访问 |
| S5 仓库整理 | tests、scripts、docs 分子阶段搬迁；全部路径消费者与 CI 特殊输入同时更新 | 收集数量、fixture 作用域、脚本根目录、CI 路径分类、文档活动链接、WORM 哈希 |
| S6 终验 | 所有阶段累计结果 | wheel 干净环境、入口/资源、升级连续性与隔离 daemon 场景；保存失败/成功证据 |

每阶段有独立 checkpoint；目录规则在对应搬迁完成的阶段才启用。`notes/` 禁令与其迁移同阶段上线，不提前使 CI 变红。首次方向门严格执行前，S1 必须已处理全部审定违规；不通过取消门或清空旧豁免获得绿色。

## 7. C0 本轮结果

### 工具实测

在 Python 3.12 下，对 1,236 个 tracked Python 文件进行解析，包括全部 354 个产品文件：

| 工具 | 全仓成功 / 失败 | 产品成功 / 失败 | 结论 |
|---|---|---|---|
| Python 3.12 AST | 1,236 / 0 | 354 / 0 | 用于扫描和结构检查 |
| LibCST 1.9.0 | 1,236 / 0 | 354 / 0 | 无操作输出全部逐字节一致；选为重写引擎 |
| Bowler 0.9.0 / Fissix 24.4.24 | 1,223 / 13 | 353 / 1 | 未覆盖本仓全部有效语法；不作为主重写引擎 |

Bowler 对照按默认 `Query(python_version=3)` 实际传入的 `print_function=True` 配置执行。初次使用 Fissix 默认语法的 88 个失败结果已被修正报告替代，不能作为工具结论。产品失败点为 `src/hyprial/transfer/smolvm.py` 的上下文管理器语法；其他失败在 contract、desktop、tests，均被 Python AST 和 LibCST 成功解析。

完整证据：`docs/evidence/refactor-c0-2026-10-01/parser-probe.json`。字节一致性只证明无操作解析/生成保持源码，不证明任何后续变换的语义正确。

### Authority 基线

三个现有静态 manifest 校验均为 0 错误；豁免条数分别为 actor 3、supervision 66、inbox 60。证据：`docs/evidence/refactor-c0-2026-10-01/authority-baseline.json`。这是三个 manifest 校验的结果，未运行全量 pytest 或真实 E2E。

### 扫描与主模型审阅

最终证据位于 [`scan-final/summary.md`](../evidence/refactor-c0-2026-10-01/scan-final/summary.md)，完整九项报告在同目录。当前工作区枚举 3,042 个 tracked 文件，比隔离基线 clone 的 3,041 多出用户已暂存的 `uv.toml`；源码统计相同。扫描器没有读取或操作真实 daemon/provider。

| 项目 | 实测 |
|---|---|
| Python 覆盖 | 1,236 个全仓文件；产品 354 个 / 212,872 行 |
| Import | 13,169 个 AST 站点；18,592 条目标/符号引用；两者不混计 |
| 静态覆盖 | 解析错误、缺失输入、未映射产品源码、未解析内部 import 均为 0 |
| 原映射违规 | 74 个独立站点 / 6 个领域方向 / 45 个源模块目标边 |
| 五模块静态 SCC | 五者仍处于同一连通分量；不等于运行时循环已被证明 |
| Ports 使用 | 产品 83 个站点，覆盖 10 个 ports 模块；原方案“无人走”不成立 |
| Authority | 3 + 66 + 60 = 129 条，完整记录身份/理由；原校验零错误 |
| CLI | 四个入口文件共 218 个注册/挂载调用候选；`cli.py` 为 144 个 command 调用、27 个 add_typer 调用 |
| Application | `DaemonApplication` 直接类成员方法 284 个；按方法记录 self 读/写/调用 |
| 无写入与稳定性 | tracked 输入哈希未变化；独立输出目录复跑逐字节比较；13 个针对性测试和局部 Ruff 检查通过 |

74 个逆向站点的分布：daemon → biz 31、identity → daemon 21、biz → shell 14、kernel → daemon 5、daemon → shell 2、identity → biz 1。kernel 统计包括根包版本文件的 9 行；daemon 仍占产品源码约 73.6%，因此“目录压成五个”不能单独证明深模块目标达成。

`cli.py` 的 144 个 command 调用包含 143 处装饰器和 1 处 `app.command(name)(command)` 动态注册调用；不能据此声称运行时只有 144 个命令。284 个方法按类 body 直接成员统计，与原方案的 356 不同；抽取规则必须采用模块 + qualified name + 哈希，不使用旧行段作为唯一定位。

引用扫描记录 440 条 `__file__` 观察、84 条 `-m` 列表/元组入口候选、186 条可解析相对模块字符串候选、30 条 import_module/__import__ 调用候选、9 条 resources.files 同名调用候选及 26,727 条文本路径候选。它们含测试与历史材料、重叠和误报，不是对应数量的改写任务；动态值、别名、资源所有者及持久化配置迁移仍须人工审定。

主模型已审阅并修正：源模块点分身份、相对/from-package 子模块逐 alias 解析、namespace package、方向判断、独立站点去重、TYPE_CHECKING alias/else、outside-product tracked 相对引用、增量字段读写、编码 cookie、输出覆盖保护、缺失/symlink 输入和扫描前后漂移检查。快模型初版的 3 条未解析记录其实是 `tests/fixtures/smolvm_daemon.py` 同一站点导入已有 `smolvm_model_evidence.py` 的三个符号，最终报告已解析，未删除证据来隐藏问题。

API/协作者报告是静态候选：调用候选基于 import 拼写，尚未证明词法绑定；字段读写不覆盖别名、容器原地修改、descriptor 或方法副作用。报告明确 `product_scan_complete=true`、`refactor_authorized_by_scan=false`，不会把 C0 完成误写成领域/API/生命周期审定完成。本轮未执行全量 pytest、构建或 E2E，也未修改产品源码。

任务分工使用仓库 `.agents/skills/hyprial-workflow/SKILL.md`；因 `hyprial ps --json` 返回 daemon 未运行，按该技能的回退规则采用内置快模型（gpt-6-luna）和独立 checkout，不启动或重配置 Harness Network。

### 后续开工前的明确输入

1. 从 74 站点逐符号确定共享契约、上层装配及需要移动的展示逻辑，生成解耦 manifest；不沿用原方案“十二处即可”的估算。
2. 审定各领域的 export allowlist、lazy 行为、已有 authority 身份和跨模块公共面规则。
3. 按 284 个方法及字段依赖审定每个协作者的状态、启动/关闭、回调与补偿所有者。
4. 为每个活动运行时入口、配置/resource、CI 特殊路径和测试根目录计算提供显式迁移规则；历史内容标记为不改写。
5. 将上述 manifest、基线与本节脚本规格交给快模型，一次实现完整 S1–S6 套件；随后主模型审阅修改、检查累计 dry-run diff，再隔离执行。本轮到规格交付为止。

### 复现命令

在仓库根目录执行；证据输出必须选择新的目录/文件，不覆盖本轮结果。

```sh
uv run --no-project --python 3.12 python scripts/refactor/c0_scan.py --root . --output /private/tmp/hyprial-c0-new
uv run --no-project --python 3.12 --with libcst==1.9.0 --with bowler==0.9.0 --with fissix==24.4.24 python scripts/refactor/parser_probe.py --root . --output /private/tmp/hyprial-parser-new.json --compare-bowler
uv run --no-project --python 3.12 python scripts/refactor/gate_baseline.py --root . --output /private/tmp/hyprial-authority-new.json
uv run --no-project --python 3.12 python -m unittest discover -s tests/refactor -p test_c0_scan.py -v
uv run --no-project --python 3.12 --with ruff==0.16.9 ruff check scripts/refactor tests/refactor/test_c0_scan.py
```

扫描使用当前 index 的 tracked 清单，所以未来把新工具/报告纳入版本控制后，全仓文件数会正常变化；比较产品范围与各输入哈希，不用固定总数冒充覆盖验证。
