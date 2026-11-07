REJECTED

WP8 第一次验收，2026-10-15。
候选：67fdcf0e9e5c1c3bf01a42be5e3590b135d25adf。
产品基线：88c24e0a273c430a546a1285b9639ed24e0c0641。
dev 对照：682ed268bfa266ef0a5659f74fa8bee1987b9183。
独立执行时间：2026-10-15 14:42–15:10 UTC。

结论

WP8 尚不可合 dev。E2E-001 缺 org 前置，契约测试缺少真实观测。E2E-026 的覆盖退化，契约及 skill 有事实错误。E2E-021 还存在集成独有的失败前沿。返工编号见末节 R1–R5。
worker_active 修复、owner 显式回复 PAC 通知、4619e9691 的测试语义更新及权威清单 64→67 均可接受。这 4 项均不构成拒收原因。
未发现必须归因于 WP8 新增产品代码的 pytest 回归。全量仍不是绿；环境失败和间歇失败均保留原始数字。

执行条件与证据位置

CPython 3.12.13，沿用本树 .venv，未更换解释器。Node v24.21.0，PATH 前置任务指定的 Node 24 目录。src/gui/node_modules/.package-lock.json 与 desktop/node_modules/.package-lock.json 已存在，未重复 npm ci。
所有 Python 命令均经 uv run。所有场景使用自建 home、socket 和端口。未连接生产 daemon 做场景测试，未使用真实模型凭据或生产 Lark App。
只新增本判词文件；未改 src/、tests/、contract/，未 push、未开 PR、未主动给 agent 发消息。
完整临时日志在 /tmp/wp8-accept-logs/。关键命令、读数和反例在本文件内保留，不以临时日志代替判词证据。
基线另克隆到 /tmp/wp8-accept-dev、/tmp/wp8-accept-intg，分别检出上述固定 SHA，并以同一 CPython 3.12.13 创建测试环境。

场景命令统一去除 HYPRIAL_MANAGED_WORKER、HYPRIAL_WORKER_ACTOR、HYPRIAL_WORKER_SESSION_REF、HYPRIAL_TOOL_PROFILE_ID，以及大小写 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY；设置 NO_PROXY=127.0.0.1,localhost，HYPRIAL_BIN 指向对应树的 .venv/bin/hyprial。
第二轮 pytest 另去除 HYPRIAL_NODE_ID。首轮发现该变量泄入 squire 测试，细节见全量归类。

逐项验收

PASS 01 焦点命令：PYTHONPATH=$PWD/src uv run --with libcst==1.9.0 --extra test pytest tests/tooling/contracts tests/tooling/ci/runner tests/shell/skills tests/shell/login/device tests/integration/native_harnesses tests/daemon/squire/template -q -n 6；清洗环境后 459 passed，0 failed，76.86 秒。K1–K15、S1–S4 均存在且无 skip/xfail 标记。末次 K/S 单跑 20 passed。
PASS 02 t11 在焦点集通过；§5.2 分类命令无输出。新增 18 个 NATIVE_ORIGINS 均逐文件登记，说明串均含 send-links 来源包：WP1、WP2-A、WP2-B1、WP2-B、WP2-B3、WP5、WP5-A、WP5-C；无目录前缀豁免。
PASS 03 按 §5.3 的退役名 git grep 扫 src、contract、scripts，并排除 contract/wire/send-links.*，无输出。
PASS 04 golden 命令末行 RESULT GREEN 35/35；validate.py 退出 0，RESULT PASS 25 envelope fixtures, 35 expectation cases, 31 coverage tags。改名仅 WG-001/002/004/005，值未变，wire.py 均读取 reply_owed。4 个新 fixture 及 WG-030–037 齐全；8 个新增和 4 个改名 case 均有 hyprialOnly:true、sources。此项只判执行结果和登记；裁判有效性另见 R2。
PASS 05a ./contract/run-all.sh --isolated-only 退出 0：CLI 12/12、golden validate PASS、E2E inventory 24、daemon lifecycle 2/2、mcp-tools 32/32、python-worker 和 user-proxy 协议均 PASS。contract/e2e/validate.py 为 RESULT PASS 24 documented E2E scenarios。
FAIL 05b run-send-links.py 独立执行和全隔离门内均 RESULT PASS E2E-026 send-links isolated pass=13 fail=0 blocked=0；但 E26-08/11 不足以证明所称性质，E26-07 未追踪同一个 held id，见 R3。不能把 13/13 当作原规格全部断言已通过。
FAIL 05c uv run --extra test python contract/e2e/run-isolated-gate.py 已全量实跑，15 个场景无一因前序失败跳过；退出 1。E2E-001 是 §3.6 遗漏，E2E-021 的 fs join 是集成独有失败前沿，不能按环境红放行。其余红逐条见下节。
BLOCKED 06 E2E-027：01/02/04 缺 HYPRIAL_TAILCAT_BINARY；05/06 缺 attended TUI；03 为 NOT-RUN。跑者诚实输出 none-passed，没有 skip 冒充 PASS。即使补齐二进制，当前跑者也只打印 NOT-RUN，尚无自动执行这些腿的实现。
PASS 07 指定的 contract-impact 集成基线命令退出 0；首行：contract-impact: merge_base=88c24e0a273c head=67fdcf0e9e5c src_files_scanned=9 deleted_lines=25 added_lines=294 candidates=1 impacts=0 declared=no。
PASS 08 src 增量共 9 个文件。8 个属于白名单；composition/links.py 是任务明确要求复核的先红后绿例外。除该例外外，未见产品行为更改；逐文件结论见下节。
FAIL 09 契约内容抽查有真实 Unix IPC、CLI、存储及 hook 发布观测。12 个必查条目已逐条核对；另按 Random('67fdcf0e9') 抽取 C04/C17/C24/C40。C47 未完成，C48 的真实旧版本连续性 BLOCKED；契约另有 C23 等事实错误，见 R4。
FAIL 10 skill 主体改写、两份 squire 模板字节相同、MCP/pi bridge 描述对等均通过。hyprial-ops 仍有不可执行的附件发送示例和 outbox --from 示例，见 R4。
FAIL 11 切片 C 四处调整并非全可接受：06 的投影调整合理；07 的首发 minting 合理但 id 关联断言退化；08 未证明 durable ack；11 允许未扫描也通过。详见 R3。
PASS 12 结构门及边界门 violations: []，ruff All checks passed!，git diff --check refactor/org-structure-constrain HEAD -- ':!docs/evidence' 清。merge-tree --write-tree 无冲突；树号及环境变动见下节。
BLOCKED 13 全量两次都已实跑：第 1 次 25 failed / 13441 passed；第 2 次 4 failed / 13462 passed。没有把间歇红算作已确认 dev 红。第 2 次末尾的两条新增红由 Git 工作树根解析错误导致，定向复测 2 passed；完整失败清单及对照见下节。
FAIL 14 §5 的合 dev 清单尚未满足。第 5/6 条不能报全量绿，第 7 条隔离门有阻断，第 9/10 条 CI/PR 为 UNKNOWN；未作远端变更。逐条清单及正文建议见末节。

环境原始红没有隐去

首个未清洗代理的焦点运行是 71 failed / 388 passed，主要为 fake OIDC 的 DISCOVERY_UNAVAILABLE。按相同命令去除代理后 459 passed；该原始红不算代码回归。
首个全量已去除 4 个 worker 变量和代理，但仍继承 HYPRIAL_NODE_ID，造成下列 20 条 squire 失败。去除该变量后，定向失败集为 2 failed / 139 passed；第 2 次全量也没有 squire 失败。
第二轮全量接近结束时，Git 自动推导的顶层目录变为共享 .git/modules/harness-bridge。观察到 extensions.worktreeConfig=true，本树 config.worktree 仅含 user，没有 core.worktree。文件均仍在本树；显式 git --work-tree=$PWD status 干净。没有恢复、清理或改写共享 Git 配置。末尾 Git 操作均显式指定本树。

全隔离门：实测结果与归类

FAIL E2E-001，158 秒：Docker 本次已成功构建并启动 A/B；phase1 在 45 秒后没有 pending request。独立重跑同样失败。A 的 daemon.jsonl 明确记录 link.service_composed spaces=0, orgAvailable=false。run-two-machine-roundtrip.py 及 _two_machine/ 没有建共享 org 或 invite/join。新增 --summary 不足以解除 org-unavailable。归 §3.6 同步遗漏，不能继续引用以前的 Docker 构建 900 秒超时作本次归因，见 R1。
PASS E2E-002，14 秒：stable=2 replayed=0 system_notice=1 receipt_obligation=0 terminal_floor=expired。
PASS E2E-003，9 秒：adapter pin restart survival，persisted/one-to-one/destroy-clears 等断言通过。
BLOCKED E2E-005，0 秒：原文 RESULT BLOCKED E2E-005 uv-install-flow needs HYPRIAL_TAILCAT_BINARY (an executable hyprial-tailcat) for the wheel build hook。场景退出 2，门驱动显示 FAIL；归缺失构建输入，不能记 PASS。
PASS E2E-007，7 秒：four-lark-adapters pass=5 fail=0 blocked=0。
BLOCKED E2E-010，195 秒：upgrade-delta old=v0.5.1@2fe93bf08c98 new=candidate@67fdcf0e9e5c 通过；安装旧 tag v0.4.0.dev0 时失败。此次原文是 Failed to install requirements from build-system.requires / Failed to download pluggy==1.7.0 / Failed to download distribution due to network timeout / UV_HTTP_TIMEOUT (current value: 30s)。归环境下载失败，非本次所观测的 tailcat 错误。整体原始 RESULT FAIL，pass=1 fail=1；C48 的真实升级链未执行完。
PASS E2E-011，15 秒：lark-app-onboarding pass=16 fail=0 blocked=0；只验隔离 seam，不代表创建真实 App。
FAIL E2E-015，131 秒：unified 腿 PASS；跨节点 routine add 报 DAEMON_ERROR: 'types.SimpleNamespace' object has no attribute 'expands'。dev 两次完整场景先失败于 borrowed codex start 30 秒超时，不能只凭同一场景 ID 就说同因。另以 routines.py 构造的相同 SimpleNamespace 调 RemoteWorkflow.admit，按 dev→候选→dev→候选顺序独立执行，4 次均在 remote/__init__.py:438 报相同 AttributeError；该检查及 namespace 构造在 dev 已存在。故具体 expands 缺口是 dev 既有产品缺陷，非 WP8；仍保留 FAIL，不伪称该腿已验通。
PASS E2E-016，123 秒：source flags、ordinary-reply control、slot dedup、restart、cleanup 全部 PASS。
PASS E2E-017，15 秒：outbox/deadletter 隔离场景退出 0。
PASS E2E-018，63 秒：13 passed / 0 failed / 0 blocked；second-arrival、back-edge、tripwire 均通过。
PASS E2E-020，35 秒：FIRST_WAVE B1/B2-ABSENT/C = 3/3；跑者明说 A/B/D NOT RUN，不扩大为全部 PAC 能力验收。
FAIL E2E-021，114 秒：候选两次均 org directory member join did not complete within 20s，最后一次 fs join timed out after 5 seconds。pre-WP8 的 88c24e0a2 同样复现。dev 两次已越过两处 join，随后失败于 real redial did not finish after sidecar discovery; map-peer=False。由此确认候选的 join 失败前沿在集成分支已存在、非 WP8 新增；但尚不能归为 dev 同一失败或单纯环境问题。集成相对 dev 的差异必须列 FAIL，根因待 R5。
FAIL E2E-024，40 秒：候选两次、dev 两次均 swallow-close-delay-0-0 forwarding-driven redial did not complete within 30s; last=[]。这是同一断言的既有失败，非 WP8 引入；未证明实际转发可用。
PASS E2E-026，39 秒：原始 RESULT PASS E2E-026 send-links isolated pass=13 fail=0 blocked=0。对原规格的覆盖保留 R3。

原始门末行：
RESULT FAIL GATE isolated-macos E2E-001=FAIL E2E-002=PASS E2E-003=PASS E2E-005=FAIL E2E-007=PASS E2E-010=FAIL E2E-011=PASS E2E-015=FAIL E2E-016=PASS E2E-017=PASS E2E-018=PASS E2E-020=PASS E2E-021=FAIL E2E-024=FAIL E2E-026=PASS

E2E-027 阶段与诚实性

BLOCKED E27-01 missing HYPRIAL_TAILCAT_BINARY；需要受控 runner 的 tailcat 与 labrelay 输入。
BLOCKED E27-02 同上；副本延迟腿未执行。
BLOCKED E27-04 同上；真实双节点 LINK_UNKNOWN ingress 未执行。
BLOCKED E27-05 missing [REAL ENV] attended TUI；需要已登录 Claude Code TUI 与 pi attach 的 attended 窗口。
BLOCKED E27-06 missing [REAL ENV] codex interactive under on-request approval；需要 attended codex TUI。
BLOCKED E27-03 原始分类 NOT-RUN，precondition present but leg not driven。二进制存在，当前脚本没有执行 provider、请求或重启断言。不能写成缺二进制，也不能写成通过。
跑者最后 RESULT BLOCKED E2E-027 none-passed，退出 2。为遵守只新增判词文件的限制，复核时仅把 BLOCKED_FILE 重定向到 /tmp/wp8-accept-logs/e27-blocked.txt；没有改写 WORM blocked.txt。
仓内 blocked.txt 的 5 种缺前置记录与本次相符，重复行只说明重复运行；E27-03 的 NOT-RUN 位于 c/e2e-027.txt，不在缺前置清单中。按裁定 76，以上未实测不阻 WP8 本身；后续真实环境验收仍需实现可执行腿，单补二进制不能解锁当前占位脚本。

契约抽查：真实形状

PASS C03 / §2、§5 / json.summaryRequiredDataKeys：真实 message.send 缺 summary 返回 SUMMARY_REQUIRED，data={field:summary,maxBytes:512,reason:missing}；513 字节返回 data={summaryBytes:513,maxBytes:512}。无 outbox 新行由 E26-01 核。
PASS C04 / §3、§5 / json.errorCodes：真实 message.read_body 不可读 id 返回 MESSAGE_BODY_UNAVAILABLE，data={messageId:absent}，没有存在性提示。
PASS C05 / §3 / json.ipc：真实 read_body 恰返回登记的 12 键，message 为正文，pending=true、fetched=true；随后 pending 行仍在。取正文不替代 reply/ack。
PASS C07 / §2 / json.pendingRowKeys：真实 pending 行恰 10 键：messageId,deliveryId,conversationId,from,to,intent,kind,summary,bodyBytes,origin；无 message/topic。
PASS C11 / §4 / json.holdReasons：真实 CLI 首发 queued=true、holdReason=LINK_MINTING；delivery.detail=minting。顶层并无 detail，只提升 queued/holdReason；契约需把层级写清，见 R4。
PASS C12 / §4 / json.holdDetails：真实无 org 时 LINK_SELF_FULL/org-unavailable；首发 LINK_MINTING/minting；E26-07 观测 LINK_PEER_FULL/awaiting-peer；K3 核 8 个 detail 的闭集。未把静态闭集检查写成 8 种条件均经真实 IPC 触发。
PASS C14 / §4、§5：真实 A 发出两条未闭合请求后，向 user:other 发送同步拒绝，code=LINK_WINDOW_FULL，data={target:user:other,detail:W}；向 owner 发送无 LINK_* 或 hold，最终 USER_DELIVERY_TIMEOUT。验证了 X1 的两用与 owner 例外，未用投递失败冒充投递成功。
PASS C17 / §4 / json.replyHoldFields：tests/daemon/links/service/test_wiring.py 的真实 application router 分支断言 held reply 的 replied=false、acknowledged=false；该集在独立复核 757 passed 内。登记表四组结果字段未被 K4 实际约束的问题另列 R2；本条未声称生产拓扑已触发 held reply。
PASS C22 / §6 / json.ipc[link.drop]：真实 drop 返回 ok/actor/peer/state=reclaiming/written=false；不存在的 peer 返回 state=absent，无 drained。计数属于事件，不挪回同步返回。
PASS C24 / §7 / json.logEvents：K8 的 link 日志闭集通过；hook 与日志区分正确。serviceStages 的内容当前与源码相符，但未被 K8 约束，见 R2。
PASS C33 / §3、§8 / json.ipc：真实 started 返回 {ok,startedAtMs}；ended 接受 reportIds 数组，返回 {ok,endedAtMs,turnCount,turnCountSinceMs,lastTurnEndedAtMs}；空数组 INVALID_ARGUMENT，错误明确 1–1000 个非空且各不超过 128 字符。
PASS C35 / §8 / json.mechanismReplyPrefixes：两个前缀各唯一，headless 机制应答及 user-proxy 真机制路径测试通过；保留 replyTo、origin.mechanism、summary。没有把 headless 模型 FINAL 文本当回复。真实载体 E27-03 仍 NOT-RUN，不能扩大这个单元/机制验收结论。
PASS C40 / §8：真实 self-request 在 turn.started 之后 read_body，再 turn.ended，收到 kind=notice、intent=system、bodyBytes=0 的行；summary 为“1 条请求未回复：<id>；回复用 reply，无需回复用 ack”。此时原请求仍 pending。
PASS C45 / §9 及 mcp-tools / json 间接登记：mcp-tools 32/32；真实 MCP server 列工具含 harness_read_body，send.to 接受数组，send/reply 的 summary 必填。Python MCP 与 pi bridge 的新增描述语义相同。
FAIL C47 / §9 及 E2E 跑者：E2E-001 没有共享 org，见 R1；不能由 inventory 24 PASS 推导语料同步完成。
BLOCKED C48 / §2 及 10-upgrade-continuity.md：真实旧版本链被旧 tag 依赖下载阻断。另独立种入 summary=null 的合成 legacy 行，再经真实 daemon 重启和 IPC 读取：kind=message，bodyBytes=13，正文“legacy 中文”原样返回。这个合成检查 PASS，只证明 null 行可读，不能替代旧版本迁移验收。

另查 C09：真实 reply 的 read_body 没有 replyTo；本次只读 SQLite 中的实际 reply payload 为 {message:answer body,origin:{via:session},replyTo:<原 id>}。因此 E26-06 不再从 read_body 找 replyTo 符合产品投影，但关联字段仍须由真实发送路径的契约测试约束。
另查 C23：实际 link.minted payload 含 atMs,lifecycle,mintedAtMs,peer,revision,side,taskId；实际 link.reclaimed 含 atMs,cancelled,drained,drainedMessageIds,lifecycle,peer,reason,released,taskId，后者没有 revision/side。send-links.md §7 把两者都写成含 revision/side，错误。
X1 两用已并列；X2 应为 holdReason，真实 reason=target-not-visible 仍是既有自由文本；X5 明说本地/远端 summary 不保证一致；X6 的 hyprialOnly/sources 均有；X12 的 pendingRulings 含 7、50、G7、X5，均未擅自裁决。

反例：绿色裁判没有约束的内容

FAIL skill 示例：在已创建的隔离 home 下，照附件示例省略 --summary，CLI 退出 1，返回 {ok:false,code:SUMMARY_REQUIRED,error:"--summary is required (at most 512 UTF-8 bytes)"}。照 outbox list --from 示例执行，退出 2，返回 {ok:false,code:INVALID_ARGUMENT,error:"No such option: --from"}。outbox list --help 也没有 --from。
FAIL K4/K8 覆盖：在同一进程包装 _registry()，仅把 sendResultFields、replyHoldFields、outboxListFields、serviceStages 四项分别改为 [definitely-not-a-real-field]，然后 pytest.main 跑整个 test_send_links_contract.py，仍 15 passed，退出 0。未编辑仓内文件。K4 只核 IPC 和 pendingRowKeys；规格要求的另外三组字段没有被消费，K8 未消费 serviceStages。
FAIL WG-032/033/036：同一进程把 _MessageQueriesMixin._ipc_message_pending_list、_ipc_message_read_body、_LinkSendMixin._link_reply_to_param 替换为直接抛 AssertionError 的函数，再 evaluate_case，三条均仍与 expect 相等。WG-032 自造 row，WG-033 自行推导 unavailableCode 并比较原始 fixture，WG-036 用新复制的 _reply_to_or_invalid；没有执行这些公开路径。
FAIL E26-11：只在测试子进程用 sitecustomize 把 _BridgeResultsMixin._settle_stranded_rows 置为 no-op，独立跑原封不动的 _step_e26_11。启动和重启两个 daemon 均记录 sweep disabled harnesses=0；原断言仍通过，输出 retainedReply=<id>,sweptReply=null,keptRequest=<id>,sweepDeferred=false。禁用被验机制不使该腿变红。
上述进程级补丁和合成数据只存在于隔离测试进程，不进入提交。一次全场景 no-op 负控在较早的 E26-07 超时，因此没有把那次运行当作 E26-11 的证据；使用的是独立重跑原步骤的结果。

切片 C 四处调整裁定

PASS E26-06：read_body 是投影，不含原始 replyTo。改为验证正文、reply intent、请求消失和 OUT 下降合理；须由 R2 的真实回复契约测试补偿关联字段覆盖。
FAIL E26-07：首发 LINK_MINTING/minting 合理。当前为了看 awaiting-peer 再发第二条，却只要求 first_id 或 held_id 任一到达，随后只等 first_id 的 outbox 清空。证据 heldId 与 released 是不同 id，无法证明所断言的 full-peer held 消息真的放行。不得用新增 send 掩盖待验消息仍 held；改追踪同一 id 的 holdReason 变化和最终放行。
FAIL E26-08：settle_hint=ack 与迟回 LINK_RECLAIMED 符合实现，但只问 hint 不证明行已消费。即使排干 ack 漏写，gate 仍可让 pending 视图隐藏该行。须补 durable 消费断言，例如实际 read_body.pending=false 或只读本场景库的 consumed 状态。
FAIL E26-11：扫描只覆盖 streaming/headless harness actor 是当前实现事实；直接允许 reply 留存，退化成“reply 的 hint 是 ack”，没有验扫描。原规格的隔离重启结算目标未完成。采用确定性假 harness 即可保持无真实 provider 前置，不要求放宽产品范围。

集成者四项专项复核

PASS worker_active：1c578ee9b 的新增分支以完整 node.owner==worker 判 borrowed owner；closed_at 的拒绝在前；无关 URI 不匹配。056b2ee1b 测试同时约束 borrowed True、workflow-owned True、stranger False、closed graph False。原 actor-name 分支未扩大。独立 tests/daemon/links 及周边复核通过，整个补充焦点集为 757 passed / 2 xfailed；两个 xfail 是旧 keep_recovery 的 BLOCKED-on-WP2，不计作通过。
PASS owner reply 通知：a93cce8f8 只在节点完成后，按节点标识匹配 request，并用该 owner 的 session fence 调 reply；不是伪造 complete、直接改账或 link add 兜底。扫至安静是为异步通知到达。模拟的是显式遵守硬切契约的 worker，合理；未证明只调用 workflow complete 的真实用户体验已修复。机制闭合方案仍待 Allen，不在本验收内修。
PASS 4619e9691，权威清单：3 条精确 owner/operation，未扩大目录或 operation 类别。
PASS 4619e9691，run-pac-public-events 与 guard：summary_from_text 保留确定性截断规则，仍核唯一消息、conversation/from/to/messageId、单一 durable receipt，未把正文断言直接删除来避错。
PASS 4619e9691，e2e012 fixture：message→summary 与 pending 硬切相符；原 marker/correlation 正反例保留。
PASS 4619e9691，LoudInbox：fetch_pending 按 recipient/only 选可见行并记录 fetched，has_fetched 真实查询集合，未把方法写成恒真；替身补齐载体协议。
PASS 4619e9691，codex capabilities：False→True 跟随 WP5-B 与裁定 57/65/77；on-request 这一前提仍在产品和 skill 中，没有扩成 never 下可用。
PASS 4619e9691，transient settlement：先断言完成 turn 没有 ack，再显式模型 ack，保留 consumed 及 attempts=[1,2]、后继 cycle 证据；非终态 retry eligibility 由 completed_turn 清除，append-only attempts 保留，符合 WP5-A。
PASS 67fdcf0e9：inbox 权威清单 64→67 可接受。净增仅 _WireEvaluator._WG_032/_WG_033/_WG_037 的 receive，都是隔离契约评估器；无业务写者扩权。migration pin 同步到总 132 条及新 hash，没有关闭 shrinking-only 门。R2 要修裁判，不要求因这 3 条登记本身返工。

src 逐文件范围

PASS application/messaging/inbox_surface/send.py：只改历史说明注释，没有行为变更。
PASS inbox/contracts/api.py：ReceiveResult.auto_reply→reply_owed 一行。
PASS inbox/service/delivery/receive.py：同一布尔表达式的关键字同步改名一行。
PASS ipc/wire.py：仅契约适配器增用例、gate 替身、replyOwed 映射；不用于正常业务收发。实现质量问题见 R2。
PASS mcp/server.py：只改 send/reply description 字符串。
PASS harnesses/pi/resources/pi_harness_bridge.ts：对应 description 字符串，无参数或执行逻辑变化。
PASS squire/SKILL-template.md、skills/hyprial-ops/SKILL.md：文档改写；前者与 docs/domains/squire 副本逐字相同。后者有 R4 的错误示例。
PASS composition/links.py：唯一白名单外行为修改，任务已明确授权复核；先红后绿修 borrowed owner 活性，符合 §1.2 门红例外。不得将其描述成“全部 src 无行为改动”。
可接受预算偏差：K 文件 458 行，超过规格 450 行 8 行，未超过硬门 1000 行；wire.py 789 行在 800 行预算内。新增精确分类与未恢复的 xfail 不混为一谈。

全量 pytest：全部失败及归类

命令均为 uv run --with libcst==1.9.0 --extra test pytest tests/ -q -n 6，Node 24 在 PATH。
第 1 次：25 failed, 13441 passed, 15 skipped, 7 xfailed, 1 xpassed，443.92 秒。
第 2 次：4 failed, 13462 passed, 15 skipped, 7 xfailed, 1 xpassed，427.00 秒。
15 skipped、7 xfailed、1 xpassed 均未折算为 E2E 通过。
以下 T01–T25 是第 1 次的完整失败集合；T26/T27 是第 2 次新出现的失败。

BLOCKED T01 tests/daemon/harnesses/actors/authority/lifecycle/test_late_pid_refresh.py::test_late_pid_backfills_record_and_orphan_registry：首次 running=False；清洗失败集通过，dev 对照通过，第 2 次全量通过。测试文件与 dev 逐字相同。间歇调度失败，归因 UNKNOWN，不能按“dev 两次红”规则宣称稳定既有红。
BLOCKED T02 tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load：10.459/15.216 < 0.70；dev 第 1 轮通过、第 2 轮同 ID 失败；候选定向及第二轮全量通过。已在两修订观察到红绿，归性能/调度间歇项，不是已确认稳定 dev 红。
BLOCKED T03 tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary：实际 SDK 1.2.0，期望 0.85.1；dev 两次、候选两次同 ID 红，环境 pin 不符。
BLOCKED T04 tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd：OSError: AF_UNIX path too long；dev 两次、候选两次同 ID 红，本机临时根路径超长。
BLOCKED T05 tests/shell/squire/setup/test_squire_setup_identity.py::test_setup_channel_change_reports_previous_account_without_clearing：HYPRIAL_NODE_ID 泄漏，清洗后通过。
BLOCKED T06 tests/shell/squire/setup/test_squire_setup_identity.py::test_stale_hint_never_lists_an_account_that_backs_a_union_override：同一 node-id 环境错误，清洗后通过。
BLOCKED T07 tests/shell/squire/setup/test_squire_setup_identity.py::test_transient_daemon_failure_after_consume_says_the_code_is_used：同上，清洗后通过。
BLOCKED T08 tests/shell/squire/setup/test_squire_setup_identity.py::test_setup_owner_open_id_renders_override_permission_error：前置 setup 被 node-id 错误拒绝，后续 steps KeyError；清洗后通过。
BLOCKED T09 tests/shell/squire/setup/test_squire_setup_review_1031.py::test_setup_hints_other_adapter_account_only_rows_without_clearing：同一 node-id 环境错误，清洗后通过。
BLOCKED T10 tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_scaffolds_template_and_preserves_every_file_byte_for_byte：同一 node-id 错误；在 dev 独立复现一次；清洗后通过。
BLOCKED T11 tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_does_not_overwrite_a_custom_skill：同上，清洗后通过。
BLOCKED T12 tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_creates_agent_and_pins_only_once_through_registry_port：同上，清洗后通过。
BLOCKED T13 tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_without_adapter_returns_explicit_deferred_onboarding：同上，清洗后通过。
BLOCKED T14 tests/shell/squire/setup/test_squire_setup_scaffold.py::test_setup_start_uses_configured_harness_model_provider_and_skill：同上，清洗后通过。
BLOCKED T15 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_creates_seven_step_squire_plan_and_is_byte_idempotent：同上，清洗后通过。
BLOCKED T16 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_supports_isolated_single_step_reruns：同上，清洗后通过。
BLOCKED T17 tests/shell/squire/setup/test_squire_setup_cli.py::test_wrong_channel_binding_code_rejects_without_partial_profile_mutation：前置 setup 被拒，steps KeyError；清洗后通过。
BLOCKED T18 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_state_files_are_private_and_channel_change_stays_unbound_locally：同一 node-id 错误，清洗后通过。
BLOCKED T19 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_rejects_channel_naming_an_unconfigured_adapter：node-id 错误先于期望的 adapter 错误；清洗后通过。
BLOCKED T20 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_accepts_channel_naming_a_configured_adapter：同一 node-id 错误，清洗后通过。
BLOCKED T21 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_warns_when_channels_json_is_absent：前置失败造成 warnings KeyError；清洗后通过。
BLOCKED T22 tests/shell/squire/setup/test_squire_setup_cli.py::test_setup_accepts_public_agent_configuration_overrides：同一 node-id 错误，清洗后通过。
BLOCKED T23 tests/shell/squire/setup/test_squire_setup_identity.py::test_setup_resolves_member_key_consumes_code_once_and_is_idempotent：前置失败造成 steps KeyError；清洗后通过。
BLOCKED T24 tests/shell/squire/setup/test_squire_setup_identity.py::test_setup_refuses_reusing_consumed_code_for_a_different_open_id：同上，清洗后通过。
BLOCKED T25 tests/tooling/contracts/test_contract_python_resolver.py::test_key_mutations_are_killed[dependency-gate]：copytree 遇 tests/fd-guard-probe-* 在复制中消失，shutil.Error/ENOENT；定向重跑及第 2 次全量通过。是临时目录复制竞态，未认定为 dev 稳定红。
BLOCKED T26 tests/tooling/release/file/test_version_file.py::test_the_search_above_can_actually_find_things：Git 根误指共享 gitdir，sweep 看不到 pyproject.toml；显式 GIT_WORK_TREE 指回本树后通过。
BLOCKED T27 tests/tooling/contracts/test_send_links_contract.py::test_k11_mechanism_reply_prefixes_exist：同一 Git 根错误，git grep 返回零条。显式指定本树后与 T26 合跑 2 passed；随后 K/S 全组 20 passed。

T05–T24 的共同错误原文关键部分：--machine 'mac-studio' does not match the daemon node id HYPRIAL_NODE_ID=...; user delivery is owned by the profile's preferredReceiver.machine and would never be claimed by this daemon。
未把 20 条环境失败算成 20 条产品缺陷，也未用一次定向绿伪称首个全量没有失败。
本次 test_pr_hygiene_git::test_git_env_scopes_the_token_to_one_command 在 dev 两轮及候选全量均未失败；不沿用集成者历史报告的失败数。

合 dev 清单逐条评估

已满足 5.1：WP5-C pi/codex、WP5-B、WP3-B 均在候选祖先中；WP8 从其后的 88c24e0a2 派生。
已满足 5.2：t11、18 个逐文件来源登记和无输出分类命令。
已满足 5.3：退役名 grep 无输出。
已满足 5.4：结构、边界、ruff、diff --check 清；须在正确 Git 工作树根下复验最终合并树。
未满足 5.5：全量两次均非 0 failed，存在环境和间歇项；详见精确数字，不能填“全量绿”。
未满足 5.6：仅 SDK pin 与 AF_UNIX 路径错误达到本次 dev 两轮同 ID 红的确认标准；heartbeat 为红绿交替，late_pid 为 UNKNOWN。原规格名单不能替代实跑归类。
未满足 5.7：轻量 contract corpus、35/35、32/32、inventory 均绿；全隔离门仍红，E26 覆盖另有 R3。
UNKNOWN 5.8：已知限制已在规格列出；未核实每项都已开卡并进入 PR 正文，不代执行者宣称完成。
UNKNOWN 5.9：没有查询或触发 #1457 CI，不能报 tests/contract-impact/skill-declaration/isolated E2E 全绿。
未满足 5.10：本报告提供可用数字与 trailers；现有 C47 文档同步及 R1–R5 尚未完成，PR 正文不能写“可合”。未改远端正文。
UNKNOWN 5.11：未改 WIP 标题，也未核 edited 后的 skill-declaration。
未满足 5.12：尚不可发“可合 dev”通知；本任务禁止发消息，未通知任何 agent。

PR 正文可引用的原始数字

焦点：459 passed / 0 failed。
补充行为焦点：757 passed / 2 xfailed；xfail 不计实测通过。
全量 1：13441 passed / 25 failed / 15 skipped / 7 xfailed / 1 xpassed。
全量 2：13462 passed / 4 failed / 15 skipped / 7 xfailed / 1 xpassed。
golden：RESULT GREEN 35/35；validate：25 fixtures / 35 cases / 31 coverage tags。
MCP：mcp-tools: PASS 32/32。
E2E inventory：RESULT PASS 24 documented E2E scenarios。
E2E-026：RESULT PASS E2E-026 send-links isolated pass=13 fail=0 blocked=0；必须同时保留 R3 的覆盖限制。
E2E-027：01/02/04/05/06 BLOCKED；03 NOT-RUN；none-passed。
隔离门：9 个 PASS、6 个驱动层 FAIL；其中 005 是场景 BLOCKED，010 是依赖下载失败，不能把这 2 条改写成 PASS。

建议 trailers 原文：
Contract-Impact: updated — contract/wire/send-links.md 与 send-links.json 记录 C01–C49；summary/read_body、holdReason、replyOwed、link/turn IPC 与显式回复语义同步，hyprialOnly 标明偏离冻结 TS 参考的用例。公开 messageId/message.reply/message.send 名称保留，删除的是载体代答路径；已知未实测项与验收返工项逐条列出。
Skill-Impact: updated — hyprial-ops 的 summary/read/link/hold/reply 规则、hyprial-workflow 的 PAC 通知读取与显式回复、hyprial-e2e 的 E2E-026/027、两份 squire 模板及 MCP/pi bridge 描述已更新；interactive codex 以 on-request 为前提。附件与 outbox 示例按 R4 修正后再宣称全部可执行。

对 origin/dev 的 contract-impact 原始结果为退出 1：src_files_scanned=126 deleted_lines=782 added_lines=13035 candidates=7 impacts=4 declared=no。命中通用键 messageId、replyMessageId、message.reply、message.send 的未改语料。使用临时正文提供说明后退出 0，输出 DECLARED RELEASE，4 impact(s) downgraded to notice；这不等于 impacts=0，也不能免除 R4 的实际语料同步。

merge-tree 与合并待办

PASS merge-tree：最终对 refactor/org-structure-constrain@f1a2077099b52eb4d4caaba322e96aa007d1fb63，git merge-tree --write-tree 无冲突，tree=34d1c9496966e4aa0b59c256fc81d675d802cb7b。集成分支在验收期间继续有文档提交，因此此树号只对应该固定对照头。
PASS 清理：独立检查没有 hyprial-e2e001-* 容器残留；本次三个测试树的 .venv 解释器及 dedicated evidence 根没有存活测试进程。失败现场仅保存在 /tmp。没有清理生产进程或 home。
接受的偏差：borrowed owner 的先红后绿产品修复；精确 3 条权威登记；K 文件预算 +8 行；E26-06 的 read_body 投影；E26-07 的首发 minting；裁定 76 允许的 E27 NOT-RUN/BLOCKED；X1–X13 按 §8.3 的既定处理。
不接受的偏差：用合成裁判替代真实 IPC，允许扫描不运行仍报已测扫描，把某场景在 dev 的不同失败拿来放行集成独有的早期失败，以及不可执行的 skill 示例。
集成者合并前：修 R1–R5；不要覆盖集成分支新增运行记录；核清 Git 工作树根；复跑真实合并树的焦点、结构、边界、ruff、contract corpus 和全隔离门。保留权威清单增长说明及 worker_active 行为例外。
合 dev 前：补齐全量归因/环境复测、E21 精确差异、PR 正文与 CI、缺失的 e2e-autoreply-audit/dev-reds 证据；按裁定继续保留 PAC 机制闭合、7/50/G7/X5 及真实环境未测事项，不把裁决建议写成已实现。

编号返工清单

R1 阻断：完成 §3.6 的 org 前置同步。首先让 E2E-001 的 Docker A/B 加入同一组织空间并等待互引可用，完整重跑；再逐个核 agent→agent 的 controlled/hosted/resident 跑者。当前 run-hosted-start、run-hosted-upgrade、run-resident-providers、run-codex-worker 等只补 summary，不能只因未执行真实环境就认定 org 前置已完成。同步 01/02/04/06/08/12/13/14 说明及 E2E README，补自动回复依赖审计。
R2 阻断：补齐 K4 三组结果字段、K8 serviceStages 的双向约束；让 WG-032/033/036 执行真实产品 router/回复路径，删除镜像 row、镜像 replyTo 校验和自行生成错误码的裁判。加入负控：错误登记、坏 pending/read_body/replyTo 产品路径必须让相应测试变红。保留 first-red 证据。
R3 阻断：E26-07 追踪同一 held 消息；E26-08 核 durable consumed/非 pending 状态；E26-11 用无需真实 provider 的确定性 harness 实际进入 startup sweep，reply/event 被扫掉、request 保持且不重跑；no-op sweep 负控必须红。E26-06 的关联字段由真实路径补测。
R4 阻断：修契约和 skill 的事实错误。user:other 不能写成与 owner 同样永不挡；minted/reclaimed payload 分开写并各测；send detail 层级和 startup sweep 的 harness 范围写准确。hyprial-ops 的附件示例补 --summary；outbox list 不接受 --from，按实际 --help 改示例并加可执行/参数测试。不能只以 token 出现证明 skill 正确。
R5 阻断归因：对 E2E-021 的 org directory join 做固定 SHA/解释器的同阶段对照。当前 dev 两次越过 join、候选两次在 join 超时、pre-WP8 集成头一次同样超时；根因未闭合。修复或给出可重复的环境归因后再评合 dev。不能仅列“dev E2E-021 也 FAIL”放行。保留 E2E-015 的 expands 已有缺陷与 E2E-024 同断言既有失败的独立证据。

本判词不要求在 WP8 偷修 PAC complete 的机制应答方案，不推翻裁定 76–82，也不授权改写已有 WORM 证据。
