# WP5-D 第一次验收 —— ACCEPTED

验收对象：分支 wp5d/send-links，worktree /private/tmp/hf-wp5d-send-links，HEAD fb0de74c5
（62459e720 先红测试 S1–S5 + 守护 G6 → 729c0c8c5 实现与现有测试适配 → fb0de74c5 证据；base e043d4a06）。
验收人：opus 验收角色，2026-10-15。git status 验收开始与结束均为 clean（本文件除外）。
上一次验收（e70bfda90，worker 仍在活动而中止）指出的 4 个漏改调用点（alarms x3、dispatch_without_pac x1）已在 729c0c8c5 修复，本次复跑为绿。

## 逐项结论

1. 范围与禁区：PASS。git diff e043d4a06..HEAD 仅触及 §1.5 所列 9 个源文件、7 个测试文件与 wp5d 证据；
   harnesses/、runtime/（含 forwards.py、results.py）、hooks/service.py、daemon/impl/links/、kernel/、biz/、contract/、ipc/wire.py、
   application/actors/sessions/ 均无改动（inbox/links/io.py 是 §1.5 明列文件，不属 links/ 禁区）。
   行数：io 386(<=390)、reactor 983(<=984)、authority 948(<=949)、operations 822(<=822)、types 374(<=378)、
   pac/actors/daemon 890(<=892)、dispatch/remote/__init__.py 1000(=1000，净增 '^+[^+]' 恰 1 行，无新 import)、
   routines 806(<=808)、shell control 252(<=256)。tests/daemon/inbox/io 直接文件 3（<=7）；结构门 violations []。
2. 行为：PASS。
   - deliver 的 summary 为必填关键字（io.py:229），在 recipient 分流（user: DM 与 inbox 行）之前 check_summary(required=True)，进入 InboxMessage(summary=...)。
   - derive_summary 为 staticmethod，等于 summary_from_text；探针 7 组文本（含 512/513 字节、多字节汉字、4 字节 emoji、100000 字节）：
     两次调用相同、<=512 UTF-8 字节、<=512 时原样、超长时以「…[全文 N 字节，用 read_body 读取]」结尾且前缀为原文前缀。
   - NotificationSender 协议与 NullSender 加 summary: str；reactor 两处调用（规划发送、resend_undelivered）、authority resend、
     operations（rework 后缀拼接之后对最终 text 派生）、routines、dispatch/remote 接收端 offer 投递均带 summary。
   - WorkflowSender、DaemonPacNotificationSender 透传；shell _WorkflowNotificationSender 只透传入参 summary，
     grep "hyprial.daemon.impl" control.py 无输出（原有的局部 summary_from_text 导入已删除）。
   - 探针（临时测试，未提交）：summary 在 inbox 行（SQL 直读）、pending_all、zenoh encode_delivery_frame/decode_delivery_frame 往返后均保持；
     ""、"   \n"、513 字节、孤立代理 "\ud800"、None、123、b"abc" 对 agent 与 user: 两类目标都抛 SUMMARY_REQUIRED（reason 分别为 empty/empty/-/invalid_utf8/missing/not_string/not_string，
     513 字节 data == {"summaryBytes": 513, "maxBytes": 512}），且 deliver_user 未被调用、inbox 无行；缺关键字为 TypeError。共 17 条探针通过。
   - rework 探针：back edge 场景的 rework 消息 summary == summary_from_text(含后缀的 text)，且 summary 含 "Explicit rework"。
3. 现有调用点：PASS。src/ 中 InboxDeliveryIoAdapter.deliver 的调用方仅 dispatch/remote、pac/actors/daemon、routines 三处，全部带 summary；
   tests/ 中直接调用 adapter.deliver 的 alarms、dispatch_without_pac、workflow_inbox_io 均已补 summary；
   各测试替身 NotificationSender 多为 **kwargs，显式签名的（test_pac_reactor、pac_actor_lifecycle、authenticated_message_sender、pac_remote_workflow 的 IO）已适配。
   alarms + dispatch 套件在下第 5 项焦点运行中全绿。
4. 测试与先红：PASS。S1 test_deliver_requires_summary、S2 test_delivered_row_carries_summary、S3 test_pac_senders_pass_a_deterministic_summary、
   S4 test_routine_and_remote_paths_pass_summary、S5 test_user_target_keeps_the_dm_path、G6 test_the_shell_workflow_sender_uses_only_the_public_face 均在。
   在 scratchpad 临时 worktree（e043d4a06 + 62459e720 的 tests/）复现：5 failed / 1 passed（G6 守护通过），与 red.txt 一致；临时 worktree 已移除。
   HEAD 上 tests/daemon/inbox/io 全绿。断言为行为性：真实 custody 协调器 → 本地传输 → inbox facade 的落库行与 pending 行，而非 spy 参数。
5. 运行：PASS（有已知红）。
   - 焦点：tests/daemon/inbox/io tests/daemon/pac tests/daemon/alarms tests/daemon/dispatch tests/daemon/delivery tests/shell/cli：1728 passed。
   - 广：tests/daemon tests/shell：3 failed, 8227 passed, 6 skipped, 5 xfailed, 1 xpassed。3 条失败均在已知红名单：
     pi controlled_loader t04-06、ipc cost heartbeat ratio、login/device t11。与 WP5-D 无关。
   - check_structural_limits violations []；check_module_boundaries violations []；uv run ruff check . All checks passed!；
     git diff --check e043d4a06 HEAD -- ':!docs/evidence' 无输出。
6. 合并就绪：PASS。见下「merge-tree」。
7. Contract-Impact：PASS（交 WP8 记录）。见下。

## 接受的偏差

- G6 测试名为 test_the_shell_workflow_sender_uses_only_the_public_face，规格 §7.7 为 test_shell_workflow_sender_uses_public_face；语义相同，接受。
- S3 先红形状为 AssertionError（assert sender.calls）：记录替身要求 summary 关键字，reactor 调用抛出的 TypeError 被 reactor 的投递 except 吞掉，
  根因仍是 TypeError；接受。S5 先红形状为 TypeError（规格写「校验部分同 S1」），接受。
- red.txt 含行尾空白：原始 pytest 捕获，按 WORM 保持字节原样，接受（--check 仅对 docs/evidence 报告）。
- gates.txt 末段「Final-tree gate rerun」引用 HEAD 5b8dd7834，该提交不在本分支历史中（推测为改写前的提交号）；本次验收已在 fb0de74c5 上独立复跑全部门禁，结论相同，接受，不要求改 WORM 证据。
- 规格 §10 要求 WP8 删除 contract/pac/context/README.md 中「PAC 投递 summary 可能为 null」——该行在本分支与 contract/ 全树中均不存在（grep 无结果）；不构成本包缺陷。
- reactor、authority、operations 用 hyprial.daemon 公共导出的 summary_from_text，dispatch/remote 与 routines 用 io.derive_summary；两者同一函数，边界门通过，接受。

## 观察（不阻断，交集成者 / WP8）

- workflow-request 通知：WorkflowSender 在本地路径把 text 整体重建为完整任务说明（约 1.5 KB），但按规格透传上游 summary，
  后者派生自重建前的 turn_text，形如「轮到你:build workflow:wf-…#build (event workflow-request:…)」。探针实测如此。
  该 summary 确定、可读、对同一 idempotency key 稳定，但不是「最终发送文本」的派生；而远端路径由接收端对重建后的全文 derive_summary，
  同类请求本地与远端 summary 不同。规格 §6 明定 WorkflowSender 透传，本包照做；是否统一由集成者 / WP8 定。
- 未有专门测试钉住 authority resend（authority/__init__.py:900）与 operations 路径的 summary；本次以代码审读与 rework 探针确认。可作后续补测，不阻断。

## merge-tree

- git merge-tree --write-tree refactor/org-structure-constrain HEAD（集成分支 e523421db）：rc 0，树 ad9b05ddc，无冲突。
  落地树中 §1.5 九个文件行数与本分支相同（remote/__init__.py 仍为 1000）；deliver 调用点集合不变，全部带 summary。
  注意：e043d4a06 不是 e523421db 的祖先（base 为 wp5/send-links 上的合并提交）。
- git merge-tree --write-tree wp5/send-links HEAD（WP5-A 进行中，353be4cf3）：rc 0，树 45aab4553，无冲突；WP5-A 自 merge-base 起未触及本包任何文件。

## 集成者在合并时要做

1. 合并后复跑 tests/tooling/architecture/identity/test_identity_construction_gate.py：本包把 routines.py:208/222/231 三条豁免行号顺移为 209/223/232，
   若集成分支或 WP5-A 同时改动 routines.py 行号需重算。
2. 合并后复核 dispatch/remote/__init__.py 仍为 1000 行（本包占用最后一行预算，任何其它包再加行即超 1000 行门）。
3. 合并后对 src/ 与 tests/ 重新 grep InboxDeliveryIoAdapter.deliver 与 NotificationSender.send 的新增调用/显式签名替身，确认带 summary。
4. 集成 PR 正文写 Contract-Impact：PAC 通知、routine 任务与远端 offer 投递产生的 inbox 行与 zenoh 投递帧开始携带 summary（字段既有、可选，取值由 null 变为非空）；
   InboxDeliveryIoAdapter.deliver 内部签名 summary 必填；contract/ 同步交 WP8；规格所指「PAC 投递 summary 可能为 null」一行在 contract/ 中不存在，WP8 无需删除，
   如需可在 contract/pac/context/README.md 新增「PAC 投递行带 summary」的正向说明。
5. 把上面「观察」第一条（workflow-request 本地/远端 summary 不一致）列入 WP8 或后续裁定。
