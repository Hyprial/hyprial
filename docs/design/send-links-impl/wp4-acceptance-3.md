# WP4 第三次验收（opus，2026-10-15）：REJECTED，返工 1 条

验收对象：wp4/send-links @ 4ffecd292（返工 2 提交 133b31666 测试先红 → 1df8fae05 修复 → 4ffecd292 证据）。
返工基点：c6a03e83d（合入集成 856bf5389 的 merge）。
清单：wp4-acceptance-2.md「集成者补充（README 裁定 63）：返工清单最终版」4 条。
环境：macOS，worktree 自带 .venv，一律 `uv run`；开工时 `git status` 无已跟踪文件改动。

结论：清单第 1、2、4 条落实，第 1 条的保留名判定本身正确。被拒原因只有一条：第 3 条的注册守卫只挡住了 AgentRegistry 的创建入口，`session.register` 能在没有 registry 行的情况下以保留名建立会话并以该身份发消息，绕过全部 link 约束；且返工 2 把远端保留名改为豁免后，这条路径的可利用面比基点更大。

## 清单逐条

1. link_governed 按保留名判 — PASS。admission.py:128-167 用 `parse_agent_uri` 取 actor 段，与 `internal_actors`（默认并由 composition 传入 `RESERVED_AGENT_ACTOR_NAMES`）精确比对，sender/recipient 任一命中即不受管。HEAD 探针：
   `agent:op:node-b:dispatch-coordinator -> agent:op:node-a:w governs= False`
   `agent:op:node-a:w -> agent:op:node-b:dispatch-coordinator governs= False`
   `agent:op:node-b:_turn-hooks -> agent:op:node-a:w governs= False`
   近似名仍受管：`_named-worker`、`dispatch-coordinatorX`、`Dispatch-Coordinator`、`turn-hooks` 均 governs= True。matrix 末项已改为 False，并补远端四例。常量在 kernel/impl/contracts/reserved_actors.py，经 kernel 公共面导出；dispatch/identity.py 仍导出 `DISPATCH_SERVICE_ACTOR_NAME`（re-export）。`git grep '"_turn-hooks"\|"dispatch-coordinator"' -- src` 在 kernel 之外无输出（只剩 docstring），无第二份保留集合。
2. 只读属性 — PASS。`internal_actors`、`gate` 在 `bound` 之后；`setattr` 两者均报 `property ... has no setter`；各一条测试（test_internal_actors_property_exposes_the_reserved_names、test_gate_property_exposes_the_bound_gate）。
3. 保留名注册守卫 — FAIL。
   - 守卫本身正确：`native_actor`（_core.py:126-156）在 `normalize_actor` 之前精确比对，裸名与本节点 URI 形式均拒，`dispatch-coordinator2`/`Dispatch-Coordinator`/`turn-hooks` 放行；抛现有 `InvalidAgentNameError`，code `INVALID_AGENT_NAME`；IPC `agent.create` 实测返回 `DaemonRequestError INVALID_AGENT_NAME`。历史行 require/destroy 有测试。`git grep hyprial.daemon -- src/hyprial/identity` 只命中两处注释，identity 不 import daemon。
   - AgentRegistry 的全部创建路径（create、create_transfer_hosted、create_host_invited、prepare_create_command 三分支，以及 agent.create IPC、`_ensure_agent`、lifecycle steps 的 CreateAgentCommand、transfer landing）都经 `native_actor`。`save()` 对新 actor 会直接 INSERT 不校验名，但现有调用方都从已有行派生；`_import_legacy_files` 是一次性导入 pre-sqlite 历史 JSON，属「历史行」，接受。
   - 绕过：`session.register` 不要求 registry 行。application/actors/sessions/registry.py:361 `manage_agent=self.agents.get(actor) is not None`，`_prepare_session_registration` 调 `_ensure_agent`（agents_registry.py:492），后者经 `_agent_name_for` → `local_actor`，名不合 `ACTOR_NAME_PATTERN`（`_turn-hooks`）或 owner/node 非本机时返回 None（agents_registry.py:516），会话照常落库；随后带 `sessionRef` 的 `message.send` 经 `_canonical_interactive_actor`（messaging/visibility.py:425-448，只校验 URI 形状）以该身份发出。实测（scratch 测试，HYPRIAL_SENDER_AUTH_MODE=enforce，gate 绑定为 `ScriptedLinkGate(known=set(), default_admit=False)`，接收方 reader 为已注册 agent）：
     - 对照 `writer`（已注册）：gate 调用 `sender_known`，投递 queued（TARGET_UNRESOLVED）。
     - `session.register {"actor":"_turn-hooks"}` → ok；`message.send` → accepted、queued False，gate 零调用。
     - `session.register {"actor":"agent:x:y:dispatch-coordinator"}` → ok；`message.send` → accepted、queued False，gate 零调用。
     - `session.register {"actor":"dispatch-coordinator"}` → 拒（守卫生效，DomainCommandError，reserved 文案）。
     同一脚本在基点代码（c6a03e83d）上：`_turn-hooks` 同样绕过（既有）；`agent:x:y:dispatch-coordinator` 受管（gate 调 `sender_known`）。即返工 2 把远端保留名改为豁免后，本机任一 IPC 客户端可用 `agent:<任意>:<任意>:dispatch-coordinator` 会话身份绕过 link 约束，这是新增面。纯 IPC 路径（`from` 不带 `sessionRef`）在 observe 与 enforce 下都以「not a registered agent」拒绝，不受影响。
   - 裁定 63 的意图是「否则任何人起一个同名 agent 即绕过全部 link 约束」；会话注册是起 agent 身份的入口之一，按本次任务口径判 FAIL。
4. 证据 — PASS。证据文件已移到 docs/evidence/org-structure-constrain-2026-10-14/wp4/，表头写本次基点 c6a03e83d 与三提交；「Item 4」记两条不写 body replyTo 的回复路径（inbox_surface/send.py 的 message.send 带 replyTo 参数、runtime/forwards.py::_reply_and_ack），各标归属（WP3-A / WP5）。

## 先红后绿

- 133b31666 只改 tests/（3 个文件，`git diff --name-only c6a03e83d 133b31666 -- src` 为空）。
- scratch worktree（`git worktree add --detach .../red3 133b31666`，再 `git checkout c6a03e83d -- src`），`PYTHONPATH=red3/src:red3 uv run --extra test pytest red3/tests/daemon/links/admission -q` → 17 failed, 59 passed；`.../test_registry.py` → 2 failed, 46 passed。与证据一致。19 条失败均为行为断言（AssertionError 16、DID NOT RAISE 1、新属性 AttributeError 2），非 ImportError。
- HEAD：`pytest tests/daemon/links/admission tests/identity/agents/registry/entity/test_registry.py -q` → 124 passed。

## 运行

- 焦点：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links tests/daemon/dispatch tests/identity tests/daemon/delivery tests/daemon/ipc/error -q` → 1358 passed, 3 skipped。
- 全量：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon tests/kernel -q` → 3 failed, 6796 passed, 5 skipped, 6 xfailed（17:14）。三条均为既有红：pi controlled_loader t04-t06；ipc cost test_four_sides_cover_a_pure_heartbeat_load；kernel test_posix_listener_keeps_request_owner_and_reserve_fd（基点代码单跑同样 1 failed，证据已列）。wait_without_polling 本次未出现。
- check_structural_limits.py exit 0；check_module_boundaries.py exit 0；`uv run ruff check .` → All checks passed!
- `git diff --check c6a03e83d HEAD`：src/ 与 tests/ 无输出；46 条 trailing whitespace 全在 docs/evidence/.../wp4/ 下的原始输出（rework-2-pre-existing-*.txt、rework-2-red-*.txt）与证据 md 一行。
- `git diff c6a03e83d HEAD -- src/hyprial/daemon/impl/links/ports.py` 为空。
- 分支未改 contract/。

## 合并就绪

`git merge-tree --write-tree refactor/org-structure-constrain HEAD`（ref = cb9de9735）→ 8cda0d981f498cb788d151d2b6fada1575dabc9f，退出 0，无冲突。

## Contract-Impact（WP8 同步）

- 证据文件的 Contract-Impact 仍只列 `state="refused"`、`LINK_UNKNOWN`、`ConsumptionState.refused`；返工 2 新增的可观察语义未列：
  - `agent.create`（及经 `native_actor` 的 transfer-hosted、host-invite、session.register 自动建 agent）对 `dispatch-coordinator` 由接受改为拒绝；`_turn-hooks` 原本就因形状被拒，同码换文案。错误码 `INVALID_AGENT_NAME` 已存在（identity/impl/agents/registry/_base.py:97），无新码；新文案「is reserved for a daemon internal actor」。contract/ 中无 agent 命名规则或该码的条目（`git grep INVALID_AGENT_NAME -- contract` 为空），WP8 决定是否补一条保留名规则。
  - kernel 公共面新增 `DISPATCH_SERVICE_ACTOR_NAME`、`TURN_HOOKS_ACTOR_NAME`、`RESERVED_AGENT_ACTOR_NAMES`（内部 API，非线上字段）。
  - 保留名豁免不限 owner/node（裁定 45/51），属 link 机制语义，随 WP8 的 link 契约一起写。

## 接受的偏差

- `_import_legacy_files` 不经守卫：一次性导入 pre-sqlite 历史 JSON，等同历史行，接受。
- `save()` 对不存在的 actor 会直接插入且不校验名：现有调用方都从已有行派生，非本次引入，接受；建议后续收口。
- `workspace_path`（_home_destroy.py:495）走 `native_actor`，历史 `dispatch-coordinator` 行调用它会报保留名错误。历史行 require/destroy 已测通过，接受，记一笔。
- 证据目录 rework-2-gates.txt 6.1 MB（150986 行，check_structural_limits 的完整 JSON），只需尾部；原始输出含 trailing whitespace。不阻断，见集成者清单。
- 同节点对端（peer ingress）以 `agent:x:y:dispatch-coordinator` 为 sender 只经 parse 即豁免：这是裁定 45 的直接结果（节点间互信），不在本次判定内。

## 集成者在合并时补

1. 第 2 次验收「接受的偏差」中 E2E permissive gate 动态 import tests 包的代码，WP2 合入时删除（仍有效）。
2. rework-2-gates.txt 截为尾部（或替换为 exit code + violations 摘要），避免 6 MB 原始 JSON 入库。
3. WP8：按上节 Contract-Impact 补保留名注册规则与 link 豁免语义。
4. 证据 md 的 Contract-Impact 段补记保留名拒绝（可在本次返工 3 一并做）。

## 返工清单

1. 【裁定 63 补全】会话注册与会话身份发送必须同样拒绝保留名：对 IPC 来源的 `session.register` / `session.register_guarded`（以及 `_canonical_interactive_actor` 规范化出的会话 actor），只要 `parse_agent_uri` 取出的 actor 段精确等于 `RESERVED_AGENT_ACTOR_NAMES` 之一，不论 owner/node，一律以 `INVALID_AGENT_NAME` 拒绝；daemon 进程内（origin None/"daemon"）的内部发送不受影响。落点建议在 `_canonical_interactive_actor`（messaging/visibility.py:425）或 session register 入口（application/actors/sessions/registry.py 的参数解析处），二选一但需覆盖 register 与带 sessionRef 的 message.send/reply 两侧，常量只从 kernel 取。测试先红：参数化 `_turn-hooks`、`agent:<owner>:<node>:_turn-hooks`、`agent:x:y:dispatch-coordinator`、`agent:x:y:_turn-hooks`，断言 session.register 返回 INVALID_AGENT_NAME 且不落会话行；再加一条端到端：绑定拒绝一切的 ScriptedLinkGate，确认无法以上述身份把消息投递给已注册 agent（基点可复现：本文件第 3 条的探针）。近似名（如 `dispatch-coordinator2`、`turn-hooks`）仍可注册。验收命令：`uv run --extra test pytest tests/daemon/links tests/daemon/delivery tests/identity -q` 全绿，证据补 Contract-Impact 一行。

## 集成者补充（2026-10-15）：返工 3 清单

1. 保留名在会话层的守卫：IPC `session.register`（`application/actors/sessions/registry.py:361` 一带，`manage_agent` 为 False 的路径）对 actor 段等于 kernel `RESERVED_AGENT_ACTOR_NAMES` 任一（精确相等，不限 owner/node）的 actor 拒绝，返回 `INVALID_AGENT_NAME`；带 sessionRef 的 `message.send` 与 `message.reply` 同样拒绝该类发送者（防止已存在的旧会话绕过）。常量只从 kernel 取，不复制。测试先红：`session.register {"actor":"_turn-hooks"}` 与 `agent:x:y:dispatch-coordinator` 两例被拒；一条端到端 deny-gate 测试（`HYPRIAL_SENDER_AUTH_MODE=enforce`、deny-all gate、注册 `_turn-hooks` 会话后 send 必须被拒且 gate 调用数为 0 之外没有消息入库）；对照组 `writer` 仍走 `sender_known`。
2. 证据整理：`docs/evidence/org-structure-constrain-2026-10-14/wp4/rework-2-gates.txt`（6.1 MB）只留结论尾部（≤200 行）；清掉该目录下 46 处尾随空白，使 `git diff --check c6a03e83d HEAD` 为空；Contract-Impact 行补一句「`agent.create` 与 `session.register` 对保留名返回 `INVALID_AGENT_NAME`（既有错误码，新错误文本）」。
3. 不改 ports.py；不动 orgfs 文件；只 git add 具体文件。
