# WP4 第四次验收（opus，2026-10-15）：ACCEPTED

验收对象：wp4/send-links @ af9ae0f0f（返工 3 提交 f2fe5e0f1 测试先红 → 3d1359f1e 修复 → af9ae0f0f 证据；上一 head 2745e9c42）。
返工基点：c6a03e83d。
清单：wp4-acceptance-3.md「集成者补充（2026-10-15）：返工 3 清单」3 条，及本次任务的 5 项核验。
环境：macOS，worktree 自带 .venv，一律 `uv run`；开工时 `git status` 无已跟踪文件改动。

结论：第三次验收拒绝的唯一原因（`session.register` 不需 registry 行，交互会话可以保留名注册并发消息、绕过全部 link 约束）已关闭。守卫在会话注册入口与带 sessionRef 的消息身份绑定处各调一次，按 actor 段精确比对 kernel 常量，返回既有 `INVALID_AGENT_NAME`。

## 逐项

1. 绕过复现 — PASS。scratch 探针（复用测试文件的 `_assemble_app`，HYPRIAL_SENDER_AUTH_MODE=enforce，`ScriptedLinkGate(known=set(), default_admit=False)`，接收方 reader 已注册）：
   - `session.register {"actor":"_turn-hooks"}` → INVALID_AGENT_NAME（文案 `actor 'agent:op:node-1:_turn-hooks' is reserved for a daemon internal actor and cannot own an interactive session`）；`agent:x:y:dispatch-coordinator`、`dispatch-coordinator` 同样被拒。会话行集合注册前后不变；随后 `message.send` 带同一 sessionRef → INVALID_AGENT_NAME；gate `sender_known_calls()==[]`；reader inbox 为空。
   - 对照 `writer`（worker-b）：send accepted、queued，gate 调用 `[(reader, worker-b)]`。
   - 守卫之前已存在的会话：探针先把 `_refuse_reserved_interactive_actor` 临时替换为空操作完成注册（会话行确实落库），恢复后 `message.send` 与 `message.reply`（对一条投给该保留身份的 pending 消息）均 INVALID_AGENT_NAME，gate 零调用，reader inbox 为空。`_turn-hooks` 与 `agent:x:y:dispatch-coordinator` 两例均通过。
   - 守卫落点：`messaging/visibility.py::_refuse_reserved_interactive_actor`；调用点 `sessions/registry.py:280`（session.register 与 register_guarded 共用，router 以 `guarded` 区分）和 `_authenticated_message_caller` 的 sessionRef 分支（在 session fence 之前；message.send、message.reply、orgfs_bridge 与 queries 共用此入口）。
   - daemon 内部发送不带 sessionRef，不经守卫：turn hook 以 `InboxTurnHookInvoker(callback_actor=...)` 直接走 inbox（wiring/transport.py:603），dispatch 身份由 `dispatch_service_actor_uri` 直接给出。`pytest tests/daemon/application tests/daemon/harnesses -q` → 1 failed, 1174 passed, 2 skipped；唯一失败为既有 pi controlled_loader t04-t06。
2. 精确性 — PASS。比对为 `name in RESERVED_AGENT_ACTOR_NAMES`（parse_agent_uri 取 actor 段，非 URI 时用原串），无前缀匹配；`dispatch-coordinator2`、`turn-hooks`、`_turn-hooks2` 仍可注册（参数化测试）。`git grep` 两个带引号的字面名在 `src/hyprial` 除 kernel 外无输出；常量只在 kernel/impl/contracts/reserved_actors.py。`git grep hyprial.daemon -- src/hyprial/identity` 只命中两处注释。错误码取 `InvalidAgentNameError.code`（identity 公共面新导出该异常，定义在 identity/impl/agents/registry/_base.py）；kernel ipc_errors 与 tests/daemon/ipc/error（FROZEN_CODES）自 c6a03e83d 起无改动，contract/ 无改动。
3. 先红后绿 — PASS。f2fe5e0f1 只改 `tests/daemon/delivery/message/test_authenticated_message_sender.py`。scratch worktree（`git worktree add --detach .../red4 f2fe5e0f1` 后 `git checkout 2745e9c42 -- src`，已移除）：7 failed, 22 passed；失败为 4 条 `DID NOT RAISE`（注册未被拒）、3 条 `assert 'STALE_SESSION' == 'INVALID_AGENT_NAME'`，均为行为断言。HEAD：29 passed。
4. 证据 — PASS。rework-2-gates.txt 85 行（1971 字节）；`git diff --check c6a03e83d HEAD` 无输出；实现 md「Rework 3」段 Contract-Impact：`agent.create` and `session.register` return `INVALID_AGENT_NAME` for the reserved names (existing error code, new error text)。
5. 运行 — PASS。
   - 焦点：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/dispatch tests/daemon/delivery tests/identity tests/daemon/ipc/error tests/daemon/application/messaging -q` → 1395 passed, 3 skipped。
   - 全量：`tests/daemon tests/kernel -q` → 3 failed, 6807 passed, 5 skipped, 5 xfailed, 1 xpassed。三条均为既有红：pi controlled_loader t04-t06；ipc cost test_four_sides_cover_a_pure_heartbeat_load；kernel test_posix_listener_keeps_request_owner_and_reserve_fd。wait_without_polling 本次未出现。
   - check_structural_limits.py exit 0；check_module_boundaries.py exit 0；`uv run ruff check .` → All checks passed!
   - `git diff c6a03e83d HEAD -- src/hyprial/daemon/impl/links/ports.py` 为空。

## 合并就绪

`git merge-tree --write-tree refactor/org-structure-constrain HEAD`（ref = cb9de9735）→ 9193f9f7f26204da0492385d364806d873f30be8，退出 0，无冲突。

## 接受的偏差

- 已提交的 send 测试使用从未注册的 sessionRef（`legacy-reserved-session`），基点代码上失败为 STALE_SESSION 而非真正的绕过；它证明守卫在 fence 之前，但不单独证明「已落库的旧会话」被拒。本次以探针补证（见第 1 项），接受；建议后续把「绕过守卫先落库、再 send/reply」作为一条正式测试。
- `message.reply` 无专门的保留名测试；与 send 共用 `_authenticated_message_caller`，探针已实测被拒，接受。
- 守卫在 `_canonical_interactive_actor` 之后运行：bare 名若不合规范会先以 INVALID_ARGUMENT 拒绝，不影响结论。
- 第三次验收列出的偏差（`_import_legacy_files`、`save()` 不校验名、`workspace_path` 对历史 `dispatch-coordinator` 行报错、同节点对端以保留名为 sender 按裁定 45 豁免）不变，继续接受。

## 集成者在合并时补

1. 第 2 次验收所列 E2E permissive gate 动态 import tests 包的代码，WP2 合入时删除（仍有效）。
2. WP8：按第三次验收 Contract-Impact 段与本分支证据补保留名注册规则（agent.create / session.register 返回 INVALID_AGENT_NAME，新文案）与 link 豁免语义。
3. 可选：补一条「旧会话已落库后 send/reply 被拒」的正式测试（见接受的偏差第 1 条）。
