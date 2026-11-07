# WP2-B3 第一次验收 —— REJECTED

- 日期：2026-10-15
- 验收者：opus 验收角色（集成者委派）
- 对象：分支 wp2b3/send-links，HEAD d033484f0（合并基 3abc47eb0；eaecd3f2f 测试先行；40903ca05 实现；d033484f0 证据）
- 依据：wp2-composition.md 全文；wp2.md §2.4、§3、§4.9、§9.5、§9.6；README 裁定 26、37、46、47、49、50、53、54、63、67、68、69
- 结论：REJECTED。接线主体正确、可合，但有两条分支引入的 CI 门红（tests/tooling）、L14 明显少于规格且证据未披露、以及一处 compose 绑定后失败路径违背 §3。返工量小。

## 逐项

1. 范围 —— PASS。git status 干净；三提交顺序为 test → feat → evidence。links/、wiring/transport.py、inbox_surface/、ports.py diff 均为空；application/__init__.py 为 0 增 28 删；composition/ 7 个文件；§8 净行数逐一相符：services +3、shutdown +10（+4 接线、+6 为 _CLOSE_UNBUDGETED_STEPS 登记）、top +12−1、views +3、pac/actors/daemon +14、admission −9、gate.py −43。
2. 五个接缝 —— PASS。orgfs 取 app._require_orgfs_runtime().facade；OrgLinkSpaces 按 orgs() 减 left 减 deleted、resolve_org_directory_space、5 s 缓存，探针：两个 org 返回 ('sp-a','sp-b')（裁定 50a 镜像，适配器不选）；无 org 时 service 绑定、orgAvailable=False，admit_send 得 Hold(LINK_SELF_FULL, detail='org-unavailable')，owner user: 放行；PresencePort 每次读 _presence 并只留 agent scheme（不调 online_actors，变异测试可杀）；publisher 为 HookEventPublisher(app._hook_bus, source="daemon.links")，lane 经 _daemon_hooks.register_consumer("links", lane, ("graph.closed","worker.cleanup.complete"))；PacLifecyclePort 每次开只读 PacGraphStore 并 close。
3. 单槽（裁定 67）—— 部分 FAIL。正常路径恰一次 admission.bind(service)；link.service_skipped 仅在槽预绑定时出现（探针 reason=slot-bound、gateType=PermissiveLinkGate）；orgfs/construct/start 失败时槽未绑定、daemon 继续（C3/C4）。缺陷：bind 成功之后的步骤（pac_task_binder、composed 日志里的 spaces()/store.index()）若抛错，except 分支会 close 掉 service，但槽仍绑着这个已关闭的 service、app._link_service 仍指向它，日志 stage 记为 "bind"。探针（monkeypatch pac_task_binder 抛错）实测：返回 None，slot bound=True，app._link_service 非 None，线程已停。违背 §3「失败时槽保持未绑定，_link_service 保持 None」。触发概率低，但属于 fail-closed 语义漏洞。
4. E2E 替身移除 —— PASS。grep E2E_PERMISSIVE / E2EPermissiveLinkGate / HYPRIAL_E2E_PERMISSIVE 在 src tests scripts contract 为 0；gate.py 保留 ScriptedLinkGate、PermissiveLinkGate、BudgetedLinkGate、permissive_admission_slot（G6）；owner 迁移 MCP E2E 用 org.create（A、B 各一）走真 link，本地连跑 3/3 通过（约 13.5 s/次）；pi headless fetch E2E 在 org.create 之后发送，通过；§9 所列 17 个文件加 test_top_inbound_awaiting_injection 共 233 passed。
5. 关停顺序 —— PASS。close_link_service 插在 _pac_graph_authority 关闭之后、_transfer_source 之前，早于 inbox、hook bus、orgfs runtime、_presence 置 None（C8 钉住）。对无 close 的替身按可调用性跳过；真 close 抛错不被吞：经 attempt() 记 daemon.close.failed 并计入 errors；超时返回 False 时抛 RuntimeError（C7 的 wedged 用例）。
6. top 字段与 PAC binder —— PASS。top.py 增 capacityW/outboundInFlight，views._ipc_top_snapshot 传 link_top_fields；DaemonPacNotificationSender.send 投递前取回调、投递成功后调用（C10）。
7. 测试 —— 部分 FAIL。C1–C10、A1–A6、L14 均能按编号在注释中找到；tests/daemon/links/composition 24 passed。先红：在 eaecd3f2f 复现为 3 个收集错误（ModuleNotFoundError），无语义先红，偏弱；为此做了 10 个变异（ledger_snapshot 置空、presence 改调 online_actors、不 detach lane、不滤 deleted、不滤 left、binder 不判 pac-、去缓存、不滤内部 actor、closed 判 active、replyTo 置 None），10/10 被测试杀死，测试是行为性的。L14 不达标：§10.4 与 wp2.md L14 要求四个中断点（submit 后 submitted 前；准入后 tick 前；清 hold 后内存合并前；排干 ack 后内存合并前）、重建后出站与已准入计数正确、已准入 id 不多占名额、旧排干 id 回复得 LINK_RECLAIMED；实际 test_restart.py 只有「直接往 ledger 写一行 open 出站，丢弃 service 再重建读回 1」及两条读口测试，没有经引擎路径产生状态、没有 admit_dispatch、没有 LINK_RECLAIMED。README-wp2b3 的偏差节未披露。
8. 运行 —— 部分 FAIL。
   - 任务给定焦点命令（含整个 tests/tooling，需 --with libcst==1.9.0）：3 failed / 1983 passed。其中 2 条为本分支引入：
     a. tests/tooling/architecture/authority/test_inbox_authority_gate.py：unmanifested inbox bypass composition/links.py:_abort:close 与 close_link_service:close（门的启发式把名为 service 的变量上的 close 视为 inbox 旁路）。
     b. tests/tooling/architecture/identity/test_identity_construction_gate.py：views.py +3 行使 legacy_user_uri 允许清单 status/views.py:784 漂到 787。
     两条在 3abc47eb0 上通过，在本分支及两个合并树上均红。第 3 条 test_pr_hygiene_git 在合并基同样红，属环境。
     worker 的焦点证据只跑了 tests/tooling/isolation（638 passed），因此漏掉。
   - tests/daemon 全量：6 failed / 6734 passed / 5 skipped / 5 xfailed / 1 xpassed，失败清单与 worker 完全一致（4 条 B2×WP3-A 交互红、pi 0.85.1 钉扎、ipc cost heartbeat 负载比）。注：任务描述写「ipc heartbeat」，实为 test_ipc_cost_counters 的 CPU 覆盖比，同一条。
   - 结构门：exit 1，仅既有 links/service.py 2011 行一条。模块边界：PASS，violations 0。ruff：2 条 F601（test_delivery.py:63、test_reply_settlement.py:70），合并基 3abc47eb0 上同样 2 条。git diff --check（排除 docs/evidence）干净。
9. 合并就绪 —— PASS（文本层）。merge-tree 对 refactor/org-structure-constrain（1a0e394ff，含 links/service/ 包）得树 ad7d090b8，对 wp2b-intg/send-links（fe8df76de）得树 47320efa4，均无冲突。links.service 导入路径在包化后不变。两个合并树上跑 tests/daemon/links + 两个 E2E + tests/tooling/architecture：分别 2 failed / 624 passed 与 2 failed / 631 passed，失败恰为第 8 项 a、b 两条。
10. Contract-Impact —— 未处理（记给 WP8）。新增/改变的已发布语义：日志事件 link.service_composed / link.service_skipped / link.service_failed / link.lane_failed / link.spaces_changed / link.org_available / link.org_unavailable / link.space_ambiguous / link.pac_unreadable / link.pac_bind_failed / link.inbound_row_ambiguous / link.local_agents_failed；删除 link.e2e_permissive_gate；top.snapshot 行新增 capacityW、outboundInFlight（未接线时为 null）；发送 hold detail org-unavailable（与 gate-unavailable 并存）。contract/ 无对应条目，提交信息无 Contract-Impact 行。

## 接受的偏差

- composition/links.py 777 行（预算 400，硬上限 1000）。通读：无死代码、无对引擎或 inbox 权威逻辑的复制；膨胀来自 §2 规定的逐字段映射（LedgerRow/InboundRow/HeldRow/UpdateLinkLedgerCommand 约 110 行）、按 ruff 格式的多行调用、以及 5 处同形的「记日志并吞异常」小函数（约 40 行，可合并为一个模块级 _log）。接受，不作返工条件；如需压缩可单独无行为变化提交。
- owner 迁移 E2E 改为「先取 A、回复 A、再取 B」：真 gate 下同一对 cwnd=1，属规格允许的适配；但失败诊断去掉了 inbox 行转储，属轻微退化，可选恢复。
- L14 放 composition/test_restart.py 而非 test_ledger.py：与 §10.4 一致。

## 合并时集成者要做

- 合并后在落地树重跑 tests/tooling/architecture（第 8 项 a、b 修好后应全绿）；identity 允许清单的行号以合并后 views.py 为准再核一次。
- WP8 补 contract 条目或在 PR 正文写 Contract-Impact（见第 10 项）。
- 证据目录已有 3 份约 6 MB 的 module-boundaries-*.json，建议后续只留摘要。

## 返工清单

1. 修 tests/tooling 两条分支引入的门红：inbox_authority_gate 对 composition/links.py 两处 close 的误报（改变量名避开启发式，或按门的规则登记到 tests/inbox_authority_exemptions.json 并说明理由）；identity_construction_gate 允许清单把 status/views.py:784 更新为 787。
2. 按 §10.4 / wp2.md L14 补齐 L14：真 SQLite + FacadeLinkInboxPort + 真 LinkService，经引擎路径产生状态，四个中断点参数化，丢弃 service 不调 close 后用同一库重建，断言出站在途数、已准入 id 再问 admit_dispatch 为 True 且不多占名额、旧排干 id 回复得 LINK_RECLAIMED；附先红（ledger_snapshot 返回 () 时前两点在途为 0）。
3. 修 compose 绑定后失败路径：bind 之后不得再有可抛错步骤（把 composed 日志字段与 binder 构造挪到 bind 之前算好，或 bind 后的步骤各自兜底），保证任何 link.service_failed 时槽未绑定且 _link_service/_link_task_binder 为 None；补一条测试（monkeypatch pac_task_binder 或 spaces 抛错）。
4. 重跑并替换证据：焦点命令按任务原文（含整个 tests/tooling，uv run --with libcst==1.9.0），README-wp2b3 的偏差节写明 L14 范围与第 3 条修复。
