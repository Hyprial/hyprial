Codex arg0 npm shim 验收判词（2026-10-16）

REJECTED

候选代码修复了普通 npm 安装的 arg0 alias 拒绝问题。
但 vendor 回退和 Node 包解析仍与真实 shim 不一致。
2 个反例均会接受 shim 不会启动的目标，须修复后再验。
本判词不修改产品代码，也不代替真实模型恢复验收。

范围与版本

- 验收分支：fix/codex-arg0-npm-shim。
- 开发基线：038763c03。
- 先红提交：c3c51fab82532aa89dca59c51ca285baca38f419。
- 修复提交：1a0d464366ac4b714b2b04e3bcdc66e73dea0763。
- 对照基线：62465dbec，使用 detached worktree。
- 环境：macOS arm64、CPython 3.12.13、Node 24.21.0。
- 真实行为依据：Codex 0.161.0 官方 npm shim。

阻断项 1：平台包缺少二进制时错误回退

位置：src/hyprial/daemon/impl/harnesses/codex/process.py:171-186。

平台包可解析时，官方 shim 只检查平台包的 vendor。
如果平台包缺少二进制，shim 报错退出，不再回退。
候选代码却继续尝试主包的 vendor，并接受该目标。
因此，允许集合包含了真实 shim 不会启动的文件。

实测场景：平台包有 package.json，平台二进制缺失。
主包 vendor 中保留可执行标记脚本。
真实 shim 退出码为 1，未启动任何标记脚本。
其 stderr 包含 “Missing optional dependency”。
候选代码却接受指向主包 vendor 的绝对 alias。

修复条件：仅在平台包解析失败时选择主包 vendor。
平台包已解析但二进制缺失时，不得改用主包 vendor。
此时只保留 launched()，并增加回归测试。

阻断项 2：包文件存在不等于 Node 可解析

位置：src/hyprial/daemon/impl/harnesses/codex/process.py:123-130。

_resolve_node_package 只检查 package.json 是否存在。
Node 的 require.resolve 还受 package exports 约束。
候选代码未检查 exports，可能选择与 shim 不同的目标。

实测场景：平台包设置 exports，仅导出 “.”。
平台包和主包 vendor 中各放置不同的可执行标记脚本。
真实 shim 无法解析平台包的 package.json，遂使用主包 vendor。
标记输出为 SHIM_TARGET=FALLBACK，退出码为 0。
候选代码却接受平台包 vendor，并拒绝主包 vendor。

修复条件：解析结果须遵守 Node 的包解析规则。
不能确定平台包解析结果时，不得凭文件存在性放行。
增加 exports 阻止 package.json 导出的回归测试。

逐项裁定

- 普通 npm 平台包：PASS，正确 vendor 的 3 个 alias 均接受。
- 普通 npm 主包 vendor 回退：PASS，与真实 shim 一致。
- 无可解析 vendor：PASS，保留 launched() 单目标。
- alias 指向其它文件：PASS，仍拒绝。
- 相对 alias：PASS，仍拒绝。
- 悬空 alias：PASS，仍拒绝。
- 非白名单名称：PASS，仍拒绝。
- 与真实 shim 的回退条件一致：FAIL，见阻断项 1。
- 遵守 Node 包解析规则：FAIL，见阻断项 2。
- 任意符号链接放行：未发生；名称与绝对路径检查仍在。
- Linux、Windows、Rosetta 混合架构运行：UNKNOWN，未实测。
- 真实 Codex 模型调用与重启恢复：UNKNOWN，未实测。

先红证据

c3c51fab8 只新增测试，提交早于修复。
在该提交的独立 detached worktree 中运行新增测试：
1 failed, 5 passed，耗时 0.47 秒。
唯一失败为 test_npm_shim_vendor_binary_is_accepted。
失败原因是合法 vendor alias 被旧的精确目标检查拒绝。
修复后的 2 次 Codex 焦点测试均为 156 passed。
末次耗时 44.29 秒。
先红测试文件在修复提交中未改动，因而先红证据有效。
新增测试未覆盖上述 2 个阻断场景。

门禁记录

结构门：FAIL，存在 2 项既有违规。
62465dbec 的 detached worktree 复现相同违规：

- src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py：1005 行，上限 1000 行。
- tests/daemon/outbox：8 个直接文件，上限 7 个。

本次产品改动没有新增结构违规，但结构门没有通过。
边界门：PASS，退出码为 0。
ruff：PASS，退出码为 0。
Codex 焦点测试：PASS，156 passed，2 warnings。
全量测试：FAIL，退出码为 1，耗时 2930.47 秒。
结果：6 failed, 12925 passed, 14 skipped, 6 xfailed。
skipped 和 xfailed 均不计入功能验收通过数。
14 项 skip 的复测原文见 notes 目录中的 skips.log。

全量失败对照

5 项失败在 62465dbec 上各单独复测 2 次，均失败。
候选代码上的对应复测也各失败 2 次，判为既有：

- Pi SDK 版本：实际为 1.2.0，断言要求 0.85.1。
- IPC 心跳成本：已计量 CPU 占比未达到 0.70。
- POSIX listener：OSError: AF_UNIX path too long。
- inbox authority 清单：forwards.py 的 alarm_emitter 未登记。
- provider 术语清单：envelope.py 的 user-provider 未列入白名单。

余下 1 项为 outbox 的 actor 重试超时，归因 UNKNOWN。
断言名称为 test_attempts_past_the_curve_ceiling_never_delete_before_ttl[actor]。
全量失败原文包含 waited 2.010s of 2.000s。
候选代码上的 2 次单项复测均通过。
62465dbec 上的 7 次单项复测也均通过。
基线首次全量的另一个 outbox 断言也出现 2 秒超时。
不同断言的失败不能证明当前断言是既有失败。
因此，不能据此宣称全量测试没有新增问题。

补齐 test 依赖后，62465dbec 的全量复跑已完成。
结果：5 failed, 12908 passed, 14 skipped, 5 xfailed, 1 xpassed。
退出码为 1，耗时 2564.48 秒。
5 个失败断言与上述既有失败完全一致。
该轮 outbox 断言通过，仍不能排除间歇问题。
首次基线环境缺少 pytest-xdist，纠正过程已记录。
原始输出分别为 candidate-full.log 和 baseline-full.log。
完整断言 ID、环境脚本与复测命令见同名 notes 目录。

验证命令

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .
uv run pytest tests/daemon/harnesses/codex -q
npm --prefix src/gui ci --ignore-scripts
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600

测试命令运行前清除了下列环境变量：

- HYPRIAL_*、HARNESS_*、GIT_CONFIG_*。
- HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 及小写形式。

NO_PROXY 和 no_proxy 均设为 127.0.0.1,localhost。
HOME、XDG 配置目录和 npm 缓存均使用临时目录。
PATH 优先使用 Node 24；GUI 依赖安装成功，审计漏洞数为 0。
单测 fixture 为每个测试建立隔离 home、state 和 loopback 网络。
本次未读取生产凭据，未操作生产 daemon。

独立复现

复现脚本：docs/notes/codex-arg0-npm-shim-acceptance-2026-10-16/reproduce.py。
脚本使用未改动的官方 shim，vendor 仅含本地标记脚本。
脚本不启动 daemon，不读取凭据，不调用模型。
脚本退出码表示执行完成，不表示候选代码通过验收。
应逐项比较 shim 输出与 alias 判定。

官方来源：https://raw.githubusercontent.com/openai/codex/rust-v0.161.0/codex-cli/bin/codex.js
SHA-256：61b0194f3bb6534439c8d26a3ed57d0805f84b884588b761795323eeb92fcf70。

复现命令如下，仍须沿用上述隔离环境：

curl --fail --silent --show-error --location https://raw.githubusercontent.com/openai/codex/rust-v0.161.0/codex-cli/bin/codex.js --output /tmp/codex-0.161.0.js
shasum -a 256 /tmp/codex-0.161.0.js
CODEX_SHIM_SOURCE=/tmp/codex-0.161.0.js uv run python docs/notes/codex-arg0-npm-shim-acceptance-2026-10-16/reproduce.py

候选代码与 62465dbec 各运行 2 次，结果各自一致。
旧基线拒绝 2 个反例中的错误目标。
候选代码新增了对错误目标的接受，属于新增行为。

Contract-Impact: 本次只新增验收文档与隔离复现材料，不改已发布字段或产品行为。
Skill-Impact: 本次不改变 agent 操作方式，无需修改 skill。
