ACCEPTED

Codex arg0 npm shim 二次验收判词（2026-10-16）

首验的 2 个阻断项均已修复。
官方 npm shim 对照与先红测试均支持该结论。
首验已通过的项目未见回归。
本判词只接受本次返工，不表示全量门禁通过或具备发版条件。

范围与版本

- 首验判词：docs/design/codex-arg0-npm-shim-acceptance-2026-10-16.md。
- 分支：fix/codex-arg0-npm-shim；开发基线：038763c03。
- 本轮被验 HEAD：fc4343e7d；首验判词提交：d95d69291。
- 本轮先红：54a9e2bc6；修复：538b7c518。
- 失败对照：62465dbec，使用干净的 detached worktree。
- 环境：macOS arm64、CPython 3.12.13、Node 24.21.0。

返工的产品与测试差异仅有 2 个文件：

- src/hyprial/daemon/impl/harnesses/codex/process.py。
- tests/daemon/harnesses/codex/native_home/home_contracts/test_codex_arg0_npm_shim.py。

返工另有读数文档。本轮验收没有修改产品或测试。

阻断项 1：PASS

平台包可解析但二进制缺失时，不再尝试主包 vendor。
允许集合仅保留 launched()。

亲自复跑首验的原始反例：平台包保留 package.json。
删除平台包二进制，并保留主包 vendor 标记脚本。
官方 shim 退出 1，stdout 为空。
stderr 包含 “Missing optional dependency”。
候选代码拒绝主包 vendor alias，也拒绝缺失的平台目标。
新增测试还验证了 launched() 的 3 个白名单 alias 仍接受。

阻断项 2：PASS

exports 隐藏 ./package.json 时，候选代码改用主包 vendor。
平台包 vendor 不再因文件存在而获得接受。

亲自复跑首验的原始反例：平台包仅导出 “.”。
平台包与主包 vendor 各放置不同的可执行标记脚本。
官方 shim 输出 SHIM_TARGET=FALLBACK，退出 0。
候选代码接受主包 vendor alias，拒绝平台包 vendor alias。
代码对无法判定的 exports 返回 None，只保留 launched()。
本结论针对原始反例，不证明全部 Node 包解析形态。

官方物料与既有 PASS 项

本轮从 npm 官方仓库下载 @openai/codex 0.161.0 tarball。
其中 bin/codex.js 与首验使用的官方源码逐字节相同。
SHA-256：61b0194f3bb6534439c8d26a3ed57d0805f84b884588b761795323eeb92fcf70。

官方物料：https://registry.npmjs.org/@openai/codex/-/codex-0.161.0.tgz。
源码：https://raw.githubusercontent.com/openai/codex/rust-v0.161.0/codex-cli/bin/codex.js。

沿用首验 reproduce.py，源码 shim 连跑 2 次，输出一致。
换成 npm tarball 中的 shim 后，输出也相同。
4 案均与候选代码的目标判定一致：

- 普通平台包：shim 启动平台包，平台 alias 接受，主包 alias 拒绝。
- 平台包不可解析：shim 启动主包，平台 alias 拒绝，主包 alias 接受。
- 平台包二进制缺失：shim 报错，两类 vendor alias 均拒绝。
- exports 隐藏 package.json：shim 启动主包，主包 alias 接受，平台 alias 拒绝。

其余原有 PASS 项经 Codex 焦点测试复核：

- 正确平台 vendor 的 3 个白名单 alias 仍接受。
- 无可解析 vendor 时仍保留 launched() 单目标。
- 指向其它文件、相对目标、悬空目标的 alias 仍拒绝。
- 非白名单名称仍拒绝，未放行任意符号链接。

上述 shim 运行仅使用本地标记脚本，不调用模型。

先红证据：PASS

54a9e2bc6 是只改测试的提交，且为 538b7c518 的祖先。
提交时间分别为 21:48:00 与 21:49:48（UTC+8）。
在独立 detached worktree 亲自执行先红提交：
3 failed, 7 passed, 2 warnings in 0.50s。

3 个失败断言恰好覆盖 2 个阻断项：

- test_npm_shim_missing_platform_binary_never_falls_back。
- test_npm_shim_exports_blocked_package_json_uses_main_vendor。
- test_npm_shim_exports_blocked_package_json_refuses_platform_vendor。

test_npm_shim_missing_platform_binary_keeps_launched 在先红时通过。
该断言固定 launched() 不应被拒绝的行为。
修复提交没有修改测试。先红与当前测试文件的 SHA-256 相同：
97085c791fe6d27ed531e49a0716af8be9e99f1e84dd0dde10aee6c1745356a3。

修复后的 Codex 焦点亲自运行 2 次：
160 passed, 2 warnings，分别耗时 45.06 秒和 49.10 秒。

门禁结果

- 结构门：退出 1，仅有下列 2 项既有违规；按本任务约定不阻断返工验收。
- 边界门：PASS，退出 0，无违规。
- ruff：PASS，验收前与交付前均退出 0。
- GUI npm ci：PASS，Node 24 下安装 12 个包，审计漏洞数为 0。
- Codex 焦点：PASS，2 次均为 160 passed。
- 全量 tests/：FAIL，退出 1，耗时 2563.36 秒。

结构门的 2 项违规在 62465dbec 上亲自复现：

- src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py：1005 行，上限 1000 行。
- tests/daemon/outbox：8 个直接文件，上限 7 个。

本轮没有新增结构违规。

全量结果：
5 failed, 12930 passed, 14 skipped, 5 xfailed, 1 xpassed, 14 warnings。
14 项 skip 和 5 项 xfail 均不计为功能通过。
14 项 skip 已按精确断言复测，原因见 skips-current.log。
非严格 xpass 为 test_cross_node_hook_reply_returns_through_origin_custody。

全量失败归因

5 个失败均为既有失败，未观察到新增失败断言。
候选全量与候选焦点均复现同样 5 个失败。
62465dbec 的 2 次焦点对照也均为同样 5 个失败。
对照没有使用失败数量代替断言和错误内容比较。

- Pi SDK：test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary。
  实际版本 1.2.0，断言要求 0.85.1。
- IPC 成本：test_four_sides_cover_a_pure_heartbeat_load。
  已计量 CPU 占比低于 0.70。
- POSIX listener：test_posix_listener_keeps_request_owner_and_reserve_fd。
  错误为 OSError: AF_UNIX path too long。
- inbox 清单：test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest。
  forwards.py 的 alarm_emitter 未登记。
- provider 术语清单：test_remaining_provider_hits_are_all_whitelisted。
  envelope.py 的 user-provider 未列入白名单。

精确断言路径与原始错误见 full.log、candidate-known.log、baseline-known*.log。

首验 outbox 超时归因

本轮全量未复现首验的 outbox 超时。
精确断言为 test_attempts_past_the_curve_ceiling_never_delete_before_ttl[actor]。
候选与 62465dbec 的 3 次单项复测均通过。

- 候选：6.24 秒、5.61 秒、7.15 秒。
- 62465dbec：7.06 秒、6.61 秒、6.84 秒。

首验错误发生在 inbox 重试完成事件的 2 秒等待处。
当时 actor 为 running，queued=0，in_flight=1，failures=0。
该错误没有证明消息遭到提前删除。

inbox、outbox 实现及 outbox 测试在两版本间没有差异。
但通过复测不能证明历史超时属于既有问题。
历史单次超时的具体根因仍为 UNKNOWN。
本轮不将历史错误改判为已修复、既有或新增。

隔离、证据与限制

验证前清除了 HYPRIAL_*、HARNESS_*、GIT_CONFIG_* 和代理变量。
HOME、XDG 目录与包缓存均使用 /tmp/hfa2 下的隔离目录。
测试 fixture 创建独立 home、state、数据库与 loopback 网络。
未读取生产凭据，未变更生产 daemon 生命周期或配置。
未 push、未开 PR、未向任何 agent 发送消息。

一次附加 skip 诊断因空参数误触发默认收集。
该诊断在 docs/ 历史材料中遇到 2 个收集错误。
错误分别为缺失脚本和退役 import。
修正参数后，显式选择的 14 个 tests/ 断言均正常给出 skip 原因。
该诊断不属于正式 tests/ 全量；没有改动历史材料来消除错误。
原始输出保留在 skip-diagnostic-empty-args.log。

本轮证据目录：
docs/notes/codex-arg0-npm-shim-acceptance-2026-10-16/acceptance-2/。
执行命令见 commands.txt；返回码见 exits.txt。
边界门摘要保留原始输出 SHA-256，完整边列表留在临时读数目录。
首验 notes 与返工读数均保持原字节。

Linux、Windows、Rosetta 混合架构运行：UNKNOWN，未实测。
真实 Codex 模型调用与重启恢复：UNKNOWN，未实测。
本轮未执行跨实现契约全集或真实环境 E2E。

Contract-Impact: 本轮仅新增验收文档与读数，不改变已发布字段或产品行为。
Skill-Impact: 本轮不改变 agent 操作方式，无需修改 skill。
