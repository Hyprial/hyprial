# WP2-B3 第二次验收 —— ACCEPTED

- 日期：2026-10-15
- 验收者：opus 验收角色（集成者委派）
- 对象：分支 wp2b3/send-links，HEAD de7ce5592（d81f8a203 首验 REJECTED → 7f142eec5 先红测试 → 3c9b52360 修复 → b420bd78b 测试 lint → de7ce5592 证据）
- 依据：wp2b3-acceptance-1.md 全文；wp2-composition.md §3（link.service_failed 语义）、§8.1、§10.4；README 裁定 26、67、69、70；README-wp2b3「返工 1」节与日志
- 结论：ACCEPTED。首验四项返工全部落实，探针未发现 B3 范围内的缺陷。合并有一处文本冲突与三处语义适配，见「合并时集成者要做」。

## 逐项

1. 权威门修复正当、不是绕门 —— PASS，判定为 (a)。
   门（tests/support/gates/inbox_authority_gate.py）对 close 的检测是名字启发式：变量名末段落在 _OWNER_NAMES（含 service）里，或带 InboxService 的构造/注解/赋值来源，就当作 inbox 旁路；它对未知类型的 service 故意保守。被报的两处：_abort 的参数是同函数里用 LinkService(...) 构造的对象（注解 LinkService | None）；close_link_service 取的是 app._link_service，这个字段只在 compose 成功时被赋成同一个 LinkService。LinkService.close 只做三件事：置停止标志、join 线程、关 LinkStore（orgfs）。它不调用任何 inbox 变更方法，inbox 只经 FacadeLinkInboxPort 访问。所以它们不是 inbox 旁路，改名只是消除歧义，exemptions.json 也没有改。
   不足：代码里没有注释说明改名原因（只写在提交信息里）。需要在合并时补一行注释，见下文。
2. identity 构造门 —— PASS。允许清单 status/views.py:784→787，与 views.py:787 的 legacy_user_uri 调用一致。按原文跑焦点命令（含整个 tests/tooling，--with libcst==1.9.0），结果 1 failed / 1991 passed；唯一的红是 test_pr_hygiene_git，属环境问题（GIT_CONFIG_GLOBAL）。
3. L14 —— PASS。test_restart.py 新增四个参数化用例：真 SQLite、真 DeliveryCustodyCoordinator/Facade、FacadeLinkInboxPort、真 LinkService（FakeOrgFs）。状态都经引擎路径产生；中断方式是不调 close，再用新的 coordinator、新的 LinkAdmission、新的 service 打开同一个库重建。断言覆盖：出站在途数（第 1、2、3 点）；已准入 unclosed=2，重复 admit_dispatch 仍为 True 且仍是 2（第 2 点）；hold 清除后不重复计数（第 3 点）；已排干 id 的回复得 LINK_RECLAIMED、settle_hint=ack（第 4 点）。变异复现：在 scratch 树里让 FacadeLinkInboxPort.ledger_snapshot 返回 ()，四个 L14 用例全红（另有两条旧用例也红）；HEAD 上 7/7 绿。
4. bind 之后的失败路径 —— PASS。bind 现在是最后一个可能抛错的步骤；binder 构造和 composed 的三个字段都在 bind 之前算好。bind 之后只剩赋值、_register_hook_lane（_LinkHookLane 的构造不会抛错，注册失败由函数自己吞掉）和 _log（同样自吞）。C11 [binder|describe] 断言槽未绑、_link_service 与 _link_task_binder 为 None、hooks 没有注册、stage 与失败步骤一致、没有 composed 日志；把 links.py 回退到 d81f8a203 的版本后，这两条用例 2/2 红。
   探针（4 条注入，全部满足同一组断言，线程也已停止）：describe 阶段让 spaces() 抛错，记 stage=describe；describe 阶段让 local_agents() 抛错，记 stage=describe；admission.bind 抛 RuntimeError 或 TypeError，记 stage=bind。
   stage 的取值 binder/describe 在 §3 列表之外，属于偏差，接受（见下）。
5. 首验 PASS 项的回归 —— PASS。tests/daemon/links/composition 和 pi headless fetch E2E 一起跑，31 passed；owner 迁移 MCP E2E 连跑 3 次，3/3 通过（每次约 13.4 s）；§9 列表中 tests/daemon 以外的文件加 links/admission 共 158 passed；E2E 替身在 src/tests/scripts/contract 中 grep 结果为 0。五个接缝、关停顺序、top 字段的源码相对首验没有变化（diff 只动了 compose 尾段与 _abort/close_link_service 的局部变量名）。
6. 运行 —— PASS。
   - 焦点命令：见第 2 项。
   - tests/daemon 全量：6 failed / 6740 passed / 5 skipped / 5 xfailed / 1 xpassed / 1 error。6 条失败与首验、worker 的分类一致：4 条 B2×WP3-A 交互红（read_body×2、settle_hint、schema_migration），pi SDK 钉扎，ipc cost 心跳 CPU 比。
   - 1 条 error 是 test_session_persistence_overload[False] 的 _fd_hygiene teardown 报告 fd 泄漏，出现在负载下；它和 codex settlement 文件一起单独连跑 3 次，均为 19/19 通过，判为负载抖动，与本分支无关。
   - 结构门：只有既有的 links/service.py 2011 行一条。
   - 模块边界：status PASS，violations 0。
   - ruff：只有 2 条既有 F601（test_delivery.py:63、test_reply_settlement.py:70）。
   - git diff --check 3abc47eb0 HEAD -- ':!docs/evidence'：干净。
   - 证据 JSON：module-boundaries-wp2b3.json 共 201 行（200 行 JSON 尾部，加一行人工注记），另两份各 200 行。
7. 合并就绪 —— 有 1 处文本冲突，按两边都保留解决后通过（见下）。

## 探针发现（不属 B3 范围，转集成者 / WP8）

- P1 drop 的崩溃一致性：在「排干 ack 之后、内存合并之前」崩溃，inbox 的 drain+ack 已经同步落盘，但 link 文件的撤回还只是 LinkStore 的内存 intent，没有 flush。所以重建后 orgfs 上的 link 仍然存在：对端发来的新请求会被 admit_dispatch 放行，并占用一个名额；不重启时同一请求被拒。本分支旧的 links/service.py 和合并树上调和过的 WP2-B（drop 改为排队、由 tick 回收）都能复现。
  第 4 个中断点只断言了 LINK_RECLAIMED 和 settle_hint，没有覆盖这一点；§10.4 也没有要求。另外，在本分支的旧引擎上，重启后对已排干 id 再问 admit_dispatch 会多占一个名额，违反裁定 70；合并树上的 WP2-B 已经修好（返回 True，但 unclosed 保持 0）。
  建议由 WP2 引擎的 owner 裁定：撤回是否要先于 drain 落盘，或者在 rebuild 时从 drained 推出 reclaimed。links/** 不是 B3 能改的范围。

## 接受的偏差

- stage 新增 binder、describe 两个取值，并把 binder 的构造挪到 bind 之前（与 §8.1 第 6、7 步顺序不同）。这是首验返工 3 的直接结果，语义更严格。WP8 写 contract 时要把这两个取值一并列入。
- L14 的第 3、4 点通过替换 service 的私有方法（_merge_apply、_emit）来模拟崩溃，与引擎内部实现耦合；合并时已经因此需要适配一次（见下）。接受，但以后引擎内部再重构时还要同步改。
- 证据裁剪：三份 module-boundaries-*.json（其中两份属 WP2-B、WP2-B2）裁成文件末尾的 200 行，已不是合法 JSON，也不再包含 violations 字段，只有末行的人工注记写着「violations: 0」。原始文件本来就因为附加了一行注记而不是合法 JSON。violations 0 已由本次重跑独立确认。接受；以后应改成只存 {status, violations} 摘要。

## 合并树结果

git merge-tree --write-tree refactor/org-structure-constrain(4c2ff4c59，包含 3913002db) HEAD 的结果是树 db6aa0b40，带 1 处冲突：
- tests/tooling/architecture/identity/test_identity_construction_gate.py 的 LEGACY_USER_URI_ALLOWLIST 一段。集成侧（WP5-D）把 routines.py:208/222/231 改成 209/223/232，本分支把 views.py:784 改成 787，两处修改行相邻。解决办法是两边都保留：取集成侧的 routines 209/223/232 加本分支的 views.py:787，这与合并树里实际的行号一致。
- links/service.py：本分支没有改动，合并后按删除处理，由集成侧的 links/service/ 包取代，没有冲突。
解决冲突后，在 scratch 合并树上跑 tests/daemon/links 和 tests/tooling/architecture，结果 3 failed / 638 passed。三条失败都是集成侧 API 变化造成的语义适配，不是文本冲突：
- a. test_compose.py 中两条 C10 用例（共三处 send 调用）：WP5-D 给 DaemonPacNotificationSender.send 加了必填参数 summary，用例需要补 summary=...。
- b. test_restart.py 的 L14 drain-acked-before-merge：调和后的 WP2-B 把 drop() 改成排队，回收在 tick 里执行，所以 _arrange_drain_acked_before_merge 要在 pytest.raises(_Crash) 块里、drop() 之后补一次 node.tick()。
两处修改后，合并树上 tests/daemon/links/composition 全部 36 passed（含 4 条 scratch 探针，探针未提交）；architecture 门（inbox authority、identity）全绿。

## 合并时集成者要做

1. 按上面的办法解决 identity 允许清单冲突（routines 209/223/232 + views.py:787），然后在落地树上重核行号。
2. test_compose.py 的 C10 三处 send 调用补 summary。
3. test_restart.py 第 4 个中断点在 drop() 之后补 node.tick()（放在 raises 块内）。
4. 在 composition/links.py 的 _abort 和 close_link_service 中各加一行注释，说明局部变量名避开 service 是为了不触发 inbox_authority_gate 的名字启发式，对象是 LinkService（只关线程和 orgfs store），不是 inbox。
5. 合并后在落地树上重跑 tests/daemon/links 和 tests/tooling/architecture，应全绿。
6. WP8：contract 补 link.service_* 事件和 stage 取值（含 binder、describe），以及 top 的 capacityW/outboundInFlight 字段；探针发现 P1 交给 WP2 引擎的 owner 裁定。
