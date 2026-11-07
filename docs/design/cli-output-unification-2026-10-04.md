# CLI 输出统一

- 日期：2026-10-04
- 基线：`refactor/reconstruct-modules@d59a58a2`
- 范围：`hyprial` 所有命令的结果、错误、过程信息与退出码。命令名和命令树不在本文范围，见
  `docs/reference/cli-top-level-command-consolidation.md`（指向 dev 的盘点 PR）。
- 归属：本次深模块重构的一部分，随 `refactor/reconstruct-modules` 合入 dev。
- 前置：#1125 把 CLI 外壳收成 `shell/impl/cli/{commands,runtime,inventory.py}`，输出层落在同一子包的
  `cli/output/`。

## 1. 裁决（Allen，2026-10-04）

| 问题 | 裁决 |
|---|---|
| 是否在本次重构中统一 | 是。重构的目标就是消除各命令各自维护的重复流程 |
| JSON 外层 | 只补 `ok`：缺 `ok` 时补 `"ok": true`，其余字段原样保留。不引入 `{ok, data}` 包装 |
| 人类可读输出 | 用通用渲染器（键值 + 表格）取代 Rich 打印 dict；重要命令保留专门渲染 |
| `ok:false` 的退出码 | 一律退出 1 |

## 2. 现状

数字来自对 `src/hyprial/shell/impl/cli/commands/` 的逐命令盘点。

**统一出口已经存在，但只统一了"写到哪"，没有统一"写什么"。**
`support.py` 的 `_execute` → `_emit` / `_fail` 是事实上的出口：193 个命令里有 149 处调用。但
`operation()` 返回的是 `Any`，JSON 还是人类可读由命令自己在返回前决定：

| 类别 | 数量 | 人类模式下用户看到的 |
|---|---:|---|
| 两种模式返回同一个 dict | 约 120 个命令 | Rich `Pretty` 打出的 Python dict |
| 命令内按 `json_output` 分支，返回拼好的字符串 | 18 个命令 | 各自手写的文本或表格 |
| `fs` 的 `_fs_run`：人类模式换一个 dict | 25 个命令 | 仍是 Rich 打印的 dict |
| 绕过出口，直接 `typer.echo` / `json.dumps` / `print` | 10 多个命令 | 各自的格式 |

绕过点：`profile list/use`、`help`、`routine templates`（含 `show`）、`routine plan`、
`workflow events/context/reset/notify resend/migration status`、`fs web`，以及
`cli/runtime/entry.py:main()` 自己渲染的参数错误。

**业务层自带一套输出器。** `biz/impl/workflow/identity.py:75-102` 的 `guard/emit/fail` 直接
`typer.echo(json.dumps(...))`，并经 `biz/interface.py` 公开。它是 shell 之外唯一导入 typer 的模块。

**JSON 外层不一致。**
- 35 处在 CLI 侧显式补 `ok`；
- 47 处原样透传 daemon 结果。daemon 的惯例是带 `ok`，但没有逐个方法核实；
- 25 个 `fs` 命令同样透传。

错误格式基本统一为 `{"ok":false,"code","error","data"?}`，只有 `routine plan` 的 schema 错误用了
嵌套的 `{"ok":false,"error":{"code","message"}}`，并且退出 2。

**`ok:false` 却退出 0**：
- `doctor` 有失败项时（`observe/doctor.py:196`）；
- 交互式 `start` 的子进程失败时（`"ok": returncode == 0`）；
- `agent transfer runtime probe`。

**真实 bug：`_execute` 吞掉 `typer.Exit` 和 `typer.Abort`。** 二者都是 `RuntimeError` 的子类，
而 `_execute` 捕获 `Exception`，于是：
- `workflow run` 在确认环节被拒绝时，`typer.Exit` 被当成错误，输出一条空消息；
- `typer.confirm` 时按 Ctrl-C，得到的是空消息的失败，而不是 `main()` 的 `ABORTED`；
- biz 的 `fail()` 先打印一个 JSON 再抛 `typer.Exit`，`_execute` 又打印一个，`--json` 下 stdout
  出现两个 JSON 值。

**过程信息的通道不一致。** 大部分走 stderr，但：
- 人类模式下 `login` 的设备码提示走 stdout；
- `restart`、`org adopt`、`workflow run` 的确认预览走 stdout；
- `transfer` 的进度只在 `--json` 时走 stderr。

## 3. 规则

实施后，以下规则对所有命令成立，并由第 6 节的门禁测试保证。

1. **一个结果。** `--json` 时 stdout 恰好是一个 JSON 对象。唯一例外是显式声明的流式命令
   （第 4.4 节），其 stdout 是 NDJSON。
2. **成功对象必带 `ok`。** 数据中已有 `ok` 就保留；没有就补 `"ok": true`。其余字段不改名、
   不包装。
3. **一种错误格式。** `{"ok": false, "code": <str>, "error": <str>, "data"?: <object>}`。人类模式在
   stderr 输出 `hyprial: <message>`，个别错误码可以注册专门的提示文案。
4. **退出码与 `ok` 一致。** 成功 0；`ok:false`（无论是错误还是结果里自带）为 1；只有 click 的
   参数解析错误为 2。
5. **通道分离。** 结果走 stdout；过程信息、警告、确认提示和确认前的预览一律走 stderr，两种模式
   相同。
6. **只有输出层写 stdout。** `cli/commands/` 只返回数据；kernel、identity、daemon、biz 不导入
   typer 或 rich，不打印。
7. **控制流异常不被吞。** `typer.Exit`、`typer.Abort` 和 `SystemExit` 原样向上传给 `main()`。

例外（白名单，写明理由，门禁按名单放行）：

| 命令 | 理由 |
|---|---|
| `mcp claude-channel`、`mcp agent-channel`、`mcp claude-channel-recover`、`mcp claude-turn-ended` | stdout 是 MCP stdio 协议本身；**R11 已把这四个入口移出 CLI 树**（模块私有进程入口 `python -m hyprial.daemon.impl.mcp.entry`，`mcp claude-turn-ended` 删除），此白名单项仅作历史说明 |
| `fs web` | 前台运行的 web 服务；`--check` 改为普通 `CliResult` |
| `daemon run`、`autoupdate run` | 前台长进程，日志不是命令结果；退出码沿用进程退出码 |

## 4. 设计

### 4.1 模块

新建 `src/hyprial/shell/impl/cli/output/`（每目录不超过 7 个文件）：

| 文件 | 内容 |
|---|---|
| `__init__.py` | 说明，导出下列公共名字 |
| `result.py` | `CliResult`、`CliStream` |
| `emit.py` | `run_command`、`emit_result`、`emit_error`、`exit_code_for` |
| `channels.py` | `progress`、`warn`、`confirm` |
| `render.py` | 通用渲染器 |
| `tables.py` | 表格与时间格式辅助：从 `support.py` 迁入 `_overview_table`、`_overview_duration` 等 |
| `errors.py` | 错误到 JSON 的转换（现 `_json_failure`），以及按错误码注册的人类提示（现 `_fail` 里 `ORGFS_CONTENT_PENDING` 的特例） |

`support.py` 删去 `_emit`、`_fail`、`_json_failure` 和表格辅助，保留 daemon IPC、home 与 socket
选择。`_execute` 保留这个名字（它在 `CliServices` 中，测试会替换它），实现改为委托 `run_command`。

### 4.2 类型

```python
@dataclass(frozen=True)
class CliResult:
    data: Mapping[str, Any]                                   # JSON 本体，原样输出（只补 ok）
    render: Callable[[Mapping[str, Any]], str] | None = None  # 人类可读；None 时用通用渲染器


@dataclass(frozen=True)
class CliStream:
    items: Iterable[Mapping[str, Any]]                        # 每项一行 JSON
    render_item: Callable[[Mapping[str, Any]], str] | None = None
```

命令的 `operation()` 只返回这两种之一。人类模式需要额外信息时（例如 `fs` 用
`with_human_web_urls` 推出网页链接），在 `render` 里推导，不再返回第二份 dict。JSON 永远是
`data`，所以两种模式的数据来源一致。

迁移期间 `run_command` 临时兼容旧返回值：`dict` 视为 `CliResult(dict)`，`str` 视为只有人类
模式的结果。第 7 节第 5 步删除兼容并启用门禁。

### 4.3 执行与输出

```python
def run_command(operation, *, json_output, allow_missing_home=False) -> None:
    try:
        if not allow_missing_home:
            services.require_initialized_hyprial_home()
        outcome = operation()
    except (typer.Exit, typer.Abort, SystemExit):
        raise                                   # 规则 7
    except KeyboardInterrupt:
        emit_error(CliError("INTERRUPTED", "operation interrupted"), json_output)
    except Exception as error:                  # CLI 错误边界
        emit_error(error, json_output)          # 输出后 raise typer.Exit(1)
    emit_result(outcome, json_output)           # ok:false 时 raise typer.Exit(1)
```

`emit_result` 的行为：
- **JSON 模式**：补 `ok` 后写一行紧凑 JSON。现有 `json_indent=2`（`targets`、`hosts`）作为
  `CliResult` 的一个格式选项保留，不影响"一个值"的规则。
- **人类模式**：有 `render` 就调用它；否则用通用渲染器。输出用 `soft_wrap` 且关闭 markup，和现在
  一样；需要颜色的渲染器显式返回 Rich `Text`，不再依赖字面的 `[red]` 标记。
- **退出码**：数据里 `ok` 为 `False` 时，输出后退出 1（规则 4）。

`entry.main()` 对 click 参数错误和 `Abort` 也改为调用 `emit_error`，不再自己渲染。

### 4.4 流式输出

`workflow events` 是目前唯一的 NDJSON 命令（`--follow` 持续输出），返回 `CliStream`：
- JSON 模式下每项一行；
- 人类模式下用 `render_item`；
- `--snapshot` 在人类模式下也走渲染，不再输出原始 JSON。

消费方 `contract/pac/events/client.py` 和 `contract/e2e/run-pac-graph-slice.py` 读取的 JSON 不变。

### 4.5 过程信息与确认

```python
def progress(event: str, message: str, *, json_output: bool, **fields) -> None: ...
def warn(message: str, *, json_output: bool) -> None: ...
def confirm(question: str, *, preview: str | None = None) -> bool: ...
```

- 三者都只写 stderr。
- `progress`：人类模式写一行文本，JSON 模式写一行 NDJSON 事件，沿用 `login --json` 现有的进度格式。
- `warn`：只在人类模式输出。JSON 模式下，警告应写进结果数据（如 `workflow run` 的 `reminders`），
  不在 stderr 重复，这沿用现有测试对 `--json` 时 stderr 为空的要求。
- `confirm`：把预览写到 stderr 后提问，提示也在 stderr，返回是否同意。何时需要确认（`--yes`、
  `--json`、stdin 不是终端）由调用方判断，沿用各命令现有的语义，不在这里统一改变：例如
  `workflow run --json` 照旧不确认，`org adopt --json` 照旧抛 `CONFIRMATION_REQUIRED`。
  拒绝时抛出对应的 `CliError`，不再在 `operation()` 里 `raise typer.Exit`。
- 被替换的调用点：
  - `login_flow.py` 的设备码和设备密钥提示；
  - `adapters.py` 和 `authorize.py` 的授权链接；
  - `lark_auth.py` 的重新授权链接；
  - `messaging.py` 的"无回复路径"；
  - `workflow` 的提醒；
  - `transfer/session.py` 的进度；
  - `_announce_setup_guidance`；
  - `restart`、`org`、`agent destroy`、`workflow run` 的确认。

### 4.6 通用渲染器

规则按顺序匹配：

1. 隐藏 `ok: true`，它只对机器有意义。
2. 标量字段渲染为对齐的 `key: value` 行，键名保持原样，不翻译驼峰。
3. 由 dict 组成的列表在 120 列内放得下时渲染为表格，列取各项键的并集，顺序取首次出现的顺序；
   放不下时每项展开为一组 `key: value`，组间空一行。空列表输出 `(none)`。
4. 嵌套 dict 渲染为带缩进的小节，标题是键名。
5. 其他列表每行一项。
6. 任何值都不截断：标识符、路径、URL 完整输出。宁可换行或展开，也不丢数据。

专门渲染器保留在原命令旁边，迁移时只改签名为 `render(data) -> str`：
`_render_targets`、`_render_hosts`、`_render_top`、`_render_top_quota`、
`_render_worktree_repository`、`_format_trajectory`、`_render_autoupdate_status`、
`_render_workflow_list`、`_render_routine_list`，以及 `org invite`、`org pending`、
`profile show/list/use`、`network exposures`、`workflow overview/missions` 的文本。

### 4.7 业务层不再输出

`biz/impl/workflow/identity.py` 的 `guard`、`emit`、`fail` 删除，并从 `biz/interface.py` 撤出：
- `guard` 的检查已经由 `run_command` 完成；
- `fail` 改为直接抛 `PacError`，由 `run_command` 渲染。`PacError` 已经带 `code` 和 `data`；
- `emit` 的调用点改为返回 `CliResult`。

`worker_binding()` 等函数如果在失败时打印并退出，同样改为抛错。

## 5. 对外可见的变化

| 变化 | 影响范围 | 处理 |
|---|---|---|
| 缺 `ok` 的成功结果补 `"ok": true` | `fs` 和部分透传 daemon 结果的命令 | 只增不减。消费方都读 `ok` 或忽略多余字段 |
| `ok:false` 的结果退出 1 | `doctor` 有失败项、`adapter scopes check` 有未声明的必需权限、`adapter scopes authorize` 无可申请项（`nothing_to_request`）、交互式 `start` 失败、`agent transfer runtime probe`、`fs web --check` 发现问题 | 同步 `contract/cli-blackbox` 中 doctor 的退出码预期；核对 `autoupdate`、`transfer` 是否依赖 doctor 或 probe 的退出码 |
| `routine plan` 的 schema 错误改为标准格式并退出 1 | `routine plan` | 同步相关测试；记录在 CHANGELOG |
| 人类模式的输出文本改变 | 约 145 个命令（原来打印 dict 的） | 人类文本不是契约；断言人类文本的测试一起更新 |
| 过程提示和确认预览改走 stderr | `login`、`restart`、`org context import`（adopt）、`agent destroy`、`workflow run`、`transfer` 的事实清单（人类模式原在 stdout） | 依赖 stdout 截取设备码的脚本要改读 stderr（仓内只有测试） |
| `workflow events --snapshot` 人类模式改为渲染 | 人类模式 | JSON 不变 |
| `routine templates show` 增加 `--json` | 新增能力 | 默认输出（YAML 原文）不变 |
| `routine templates show` 未知模板退出 1（原退出 2），`--json` 为标准错误格式 `TEMPLATE_NOT_FOUND` | `routine templates show` | 同步测试 |
| `fs web --check` 改为普通结果：问题列表在 stdout，增加 `--json` | `fs web --check` | 原来问题写 stderr；服务本身的前台运行不变 |
| `workflow run` 拒绝确认时报 `CONFIRMATION_DECLINED`（原为无输出退出 1） | `workflow run` 人类模式 | 退出码不变 |
| 双 JSON 输出的 bug 修复 | `workflow` 在 `--json` 下的失败路径 | 修复 |
| `biz` 公共面去掉 `emit`、`fail`、`guard` | 只有 shell 内部使用 | 无外部影响 |

不变的：
- 所有已有 JSON 字段的名字和含义；
- 错误码；
- `help --json` 的 `usage`；
- 桌面端 Rust 读取的 `onboarding`、`org join`、`org pending` 字段；
- transfer 经 SSH 读取的 `ok` 与各字段；
- MCP stdio 协议。

PR 正文写明 `Contract-Impact`，并同步 `contract/cli-blackbox/README.md` 的退出码说明。

## 6. 门禁

新增 `tests/shell/cli/presentation/test_cli_output_discipline.py`，用 AST 检查：

1. `shell/impl/cli/commands/**` 中没有：
   - `print(`、`typer.echo(`、`typer.secho(`；
   - `sys.stdout` 或 `sys.stderr` 的写入；
   - `rich.console.Console(`；
   - 作为输出参数的 `json.dumps(`。

   写文件用的 `json.dumps` 不受限：只检查直接传给上述输出调用的情况，以及赋值后被它们使用的情况。
2. kernel、identity、daemon、biz 不导入 `typer` 或 `rich`。
3. 第 3 节例外表中的命令按名单放行。名单是测试里的常量，新增例外必须改测试并写明理由。
4. 每个带 `--json` 选项的命令，都经 `run_command`（或 `_execute`）返回。用 CliRunner 对全部命令
   做一次 `--help` 级别的注册检查，再对每个 `@command` 函数体做 AST 检查。

另加行为测试：
- `run_command` 透传 `typer.Exit` 和 `typer.Abort`；
- 补 `ok` 的规则；
- `ok:false` 退出 1；
- 通用渲染器的各条规则；
- `confirm` 在 JSON 模式下抛 `CONFIRMATION_REQUIRED`；
- `CliStream` 输出 NDJSON。

## 7. 实施步骤

每一步一个提交，独立可测；全部在同一个 PR 内完成。

1. **输出层与 bug 修复。** 新建 `cli/output/`，`_execute` 委托 `run_command`，暂时兼容旧返回值。
   先写失败测试，覆盖三种情况：吞掉 `typer.Exit`、吞掉 `Abort`、`--json` 下输出两个 JSON 值。
2. **规则 2 和规则 4。** 补 `ok`；`ok:false` 退出 1；`entry.main()` 改用 `emit_error`。同步
   contract。
3. **迁移专门渲染的命令。** 18 个自拼文本的命令和 25 个 `fs` 命令改为返回 `CliResult(data, render)`；
   其余约 120 个命令改为返回 `CliResult(data)`，由通用渲染器输出，并更新断言人类文本的测试。
4. **收掉绕过点。** `profile`、`help`、`routine templates/plan`、`workflow events/context/reset/notify/migration`、
   `fs web --check`；删除 biz 的输出器；过程信息和确认改用 `channels.py`。
5. **去掉兼容并启用门禁。** `run_command` 只接受 `CliResult` 或 `CliStream`；加入第 6 节的门禁。
6. **文档。** 更新 `AGENTS.md` 的约定段落（新增一条"CLI 输出只经 `cli/output`"）、
   `docs/guides/operator-guide.md` 中 login 输出的说明，以及 `contract/cli-blackbox/README.md`。

## 8. 验证

| 检查 | 命令 |
|---|---|
| 结构门、边界门、ruff | `check_structural_limits.py`、`check_module_boundaries.py`、`uv run ruff check .` |
| 焦点测试 | `tests/shell/cli`、`tests/shell/login`、`tests/biz/workflow`，以及所有用 CliRunner 的测试目录 |
| 全量测试 | `uv run --with libcst==1.9.0 pytest tests/` |
| CLI 黑盒契约 | `./contract/run-all.sh --isolated-only` |
| 桌面端解析 | `cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml` |
| 人工抽查 | 在隔离 home 下逐组运行代表命令，人类模式与 `--json` 各一次，记录到 PR |

未实际运行的项在 PR 中标为 UNKNOWN 或 BLOCKED。

## 9. 不做什么

- 不改命令名、不调整命令树（见命令盘点文档）。
- 不改任何已有 JSON 字段的名字或含义，不引入 `{ok, data}` 包装。
- 不改 daemon IPC 的返回格式；外层规则只在 CLI 输出时生效。
- 不给 GUI 或桌面端另做一套输出格式。
