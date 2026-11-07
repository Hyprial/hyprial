# intg-a 执行记录（2026-10-28）

最新合入顺序（用户转达研发负责人裁定）：**v0.5.5.1.1 热修 tag → 同步新 dev →
rd-be-worker6 五项 CLI 命令树归并 PR 合入本容器 → 最终独立验收 → 合 dev**。
A05 已整合、公共基线可消费，#2015 仍 Draft；不再声称只差最终判词。
worker 计划 10-29 首推、10-30 GREEN，结果未交付；本线不重复派单。
OrgFS UNKNOWN 已具名跟踪，记录、review 锚点和消息协调见
[最终交接开放项](../../evidence/cli-command-surface/a05-final-handoff-open-items.md)。
新版 devdfee 的六个 GUI/文档文件留待热修 tag 后统一同步；不会重复追逐 dev。

已完成的 dev 同步收尾：`dev@f165ed0f2` 已无冲突合入 `278a53a8`，并推送 intg。
37 Python launcher、114 GUI、typecheck/build、Ruff/结构/边界及首次 dev seed 契约门 PASS。
该 source 的新标准 wheel SHA256 `2fd82b21fe88c384e51334e1336201cb9d7204322e59e51175ad68c801f11fb8`
安装13例 PASS，三个负控 exit1；未重复全套、未加入 daemon 业务修复。
详见 [同步收据](../../evidence/cli-command-surface/a05-dev-sync-278a53a8.md)。
PR2015 正文已纠正旧“A05开发中/安装UNKNOWN”；此冻结基线等待独立scope判词，
整容器另有上方新确认的命令树和发布顺序前置。
收尾图 `wf-07c6e54400e84153823a3aa76cb249bc` 使用原 DeepSeek worker 构建，
在线 `cc-daemon-request-review` 独立评估 readiness；原 A 图不重复 reset。
此段替代下方历史记录中的“待同步dev/旧069是最新产物”；原全套FAIL与OrgFS UNKNOWN仍保留。

最新整合：按用户纠正，A05 已由 merge `0d3c8b63a35f5ab847db3866171bf30930902721`
合入 `intg/cli-command-surface`，worker head 为 `2fd98903a`。合并后的非文档树与独立
验证候选 `69c6d9404` 完全相同；实际 intg head 的 request-contract、Ruff、结构、
模块边界四门均 PASS，详见 [合并收据](../../evidence/cli-command-surface/a05-intg-merge-0d3c8b63a.json)。
此前把独立质检等待前移到实现 PR → intg 是协调错误，现已纠正；独立质检和已保留失败
的最终处理用于汇总 intg → dev，不再阻塞 A05 内部整合。两项 daemon 业务修复仍不在 A。

最新收尾记录：[A05 scope review](../../evidence/cli-command-surface/a05-scope-review-final-069.md)。
产品069，worker2fd，独立候选69c；安装13例、实际契约门、Ruff/结构/边界通过。
完整套件原始结果17F/16777P保留：13项测试清单漂移已由纯数据修正和独立214P关闭，
1项运行器PATH错误原测复验通过，2项daemon问题已在未改dev复现并移出A；
OrgFS全量偶发失败经head/base各2次复验均通过，原因仍UNKNOWN。
不再将owner迁移/route并发实现塞入A。A05最终验收及#2015 WIP保持开放，
交独立质检评估保留证据；不为纯数据/文档差异再次重跑全套或重建包。

## 授权与基线

用户已明确批准：先在已启用的离线 Kanban 挂好全部任务，再用 PAC 执行 intg-a；所有开发、测试修复与返工使用 **DeepSeek Flash Max**，codex-daemon-request 负责协调和独立验收，不合格退回直到合格，具备条件才并行。
本授权替代旧文档中 A 待批准的状态，不批准 B 的未定业务契约或 C 的全量实现；C 仍保留 Allen 审阅门。

- 设计依据：#2010@c51a747c32a2ee99a517efdfb7cf4144c92c4d18；A 范围为 cli-surface.md §7 的五卡。
- 容器：intg/cli-command-surface → dev，从 dev@9d0f5a5e7ae7e0e23b24586e23055b5f26f101e1 建立；源码盘点的227/195仍是旧基线读数，不能当作新 head 的自动验收。
- 从设计分支只带入设计材料；当前 dev 已退役旧 GUI，不因接回 #1967 恢复已删除的 GUI/post-install 文件。本批不做 GUI 产品实现。
- 协调者/验收者：agent:amy.williams@example.com:devusr-dev:codex-daemon-request。
- 开发显式配置：harness=pi、provider=deepseek、model=deepseek-flash、native args `--thinking max --approve`。禁止 tier 默认值或静默换模型。

## 已建任务

Kanban 为本机既有离线板，project=harness-bridge，owner=devusr，by=codex-daemon-request。
本次使用仅含公共 UDA/report 的临时 taskrc 及既有 data=/Users/amyws/.task；未修改用户配置、未读同步凭据、未触发同步。
先查询旧板179条项目记录，无本次五卡，再通过 kanban 动词建卡。恢复同步时按运维流程核对，不重复导入。

| 卡 | Kanban UUID | group-internal OrgFS | 完整交付依赖 |
| --- | --- | --- | --- |
| A-01 | 7955c51b-a0d9-49f9-aa98-3c7cfe4e68f0 | M-codex-daemon-request-0001 | — |
| A-02 | 2188b4ed-b5c5-4fe7-a9ba-d1fe9577a393 | M-codex-daemon-request-0002 | A-01 |
| A-03 | a3365354-71bd-4b2b-bd45-c0a714064b23 | M-codex-daemon-request-0003 | A-02 |
| A-04 | 17b9dbb6-7401-487c-856b-eb53067edbfb | M-codex-daemon-request-0004 | A-02,A-03 |
| A-05 | 1b3c8264-f0ad-43a0-be35-1b1bf04dbce8 | M-codex-daemon-request-0005 | A-03,A-04 |

每卡四项可判定验收，详见 work-packages.json；开始时 todo、未勾。模型worker只提交证据，不自行勾验收或归档；协调者按真实独立验收回填。
A-04 可在 A-02 验收后进行独立清单/测试准备（a04-prepare），A-03 合入后完成 a04-build；仍是一张卡、一个 PR。

## 工作与验收纪律

1. 每个开发worker独立worktree/分支，开始先 fetch 并将最新 origin/intg/cli-command-surface merge-forward，记录基线SHA；禁止改他人worktree/生产状态。
2. 先读本仓AGENTS、design.md、cli-surface.md、对应工作包和#1967来源说明。命令行参数/交互/渲染在CLI，传输走kernel公开client，业务留权威领域；不加旧import/alias/shim、测试专用重导出、空schema或假handler。
3. 所有Python通过uv。改结构前运行结构/边界门。焦点测试→全套tests/（LibCST按AGENTS叠加）→Ruff→适用contract隔离语料；输出日志到隔离临时目录，证据摘要入docs/evidence/cli-command-surface/。不读/改生产home/socket/凭据，不启动真实agent迁移。
4. 行为变更先红后绿；机械迁移保留行为与输出字节。真实场景PASS/FAIL/BLOCKED，未测UNKNOWN；blocked/skip不算验收通过，不能删测/放宽契约/改锁消红。
5. 每任务一个PR，base=intg/cli-command-surface；正文第一行Submitted-by真实worker URI，写动机、范围、命令/读数、红证据、未测项、Contract-Impact与Skill-Impact。不得自行合入dev或容器。
6. worker可push自身分支；fj若无本agent认证，不读/拷贝token、不改auth配置，将完整PR正文文件/标题/head/base交协调者代开。所有问题/进度/结果仅回协调者，不直接联系用户/Allen/其他开发者。
7. 第一可核查产出10分钟内：基线、改动清单、焦点测试选择和首个证据路径；有持续工作每20分钟报告检查点。证据完成后显式workflow complete，reason-ref绑定仓库文件/提交，output-text给分支SHA/PR和测试摘要。只有回消息不算完成节点。
8. build完成表示候选交审，不代表卡验收通过。review→accept/reject由协调者控制；不合格的判词落仓库，触发显式back edge，worker先reset自己的旧build flag再使用新requestId返工；协调者reset自己旧review/accept/reject旗标并复审改变后的输入，不重用旧request。
9. owner的accept只在独立复验和实现PR合入intg后置位；后续任务只依赖accept。最终在同步dev后的确切intg head验收安装产物，解除WIP后交独立质检合入dev，协调者不自并汇总PR。

### A01 后续任务采用的验证方式

先完成可执行代码和焦点验证，再固定提交运行完整套件；全套运行期间不继续改变所测源码。纯文档/注释修正可以另提交，但须写清完整运行的原SHA、精确差分及修正后的相关复验，不能冒称全套在新head运行。最终A05仍验证合并后的准确head。

长测试将stdin接`/dev/null`，stdout/stderr全部重定向到本任务独立日志，并记录PID、cwd、启动SHA及最终退出码；后台任务不得继承工具管道导致反馈被长时间阻塞。可使用锁定依赖已有的pytest-xdist，A01采用`-n 2 --dist=loadfile --max-worker-restart=0`。只清理确认为自己隔离测试所有的进程/home，不能按进程名称批量杀生产daemon。结构/边界检查的完整JSON很大，存日志后只报告状态与违规列表。

依赖镜像若返回403，可用`uv export --locked --extra test --no-emit-project`导出原锁定版本/哈希，再以`uv pip sync --require-hashes --index-url https://pypi.org/simple`装入各自独立venv，随后`uv pip install --no-deps -e .`和`uv run --no-sync`执行。需要LibCST叠加时，临时为该条uv命令提供`--no-config --index https://pypi.org/simple --default-index https://pypi.org/simple --with libcst==1.9.0`；不修改根锁、全局配置或HOME，也不共享可写venv。

已实测的dev基线问题、精确测试ID和CI日志摘要见`docs/evidence/cli-command-surface/coordinator-baseline-2026-10-28.md`。每个新失败仍需逐名归因；不能拿旧job红灯概括新失败。已退役GUI的CI根路径由A04同步，现有其他域产品失败只报协调者确认归属，不在公共框架任务里顺手改业务。

2026-10-28补充发现：已有公共CI伴随PR #1997（`work/gui-retire-common-ci-20261027`→dev，申报head `d2a662bc84825f19aac67da06963224320fd281c`）处理旧GUI根存在/缺失两种树形及Python任务的npm前置。A04开始时先核对该PR与最新dev，引用已交付的结果，避免重复修改。该PR明确未改gui.yml/desktop工作流，不能仅凭其旧基线测试通过就假定当前退休后的所有CI引用已修正；A04仍核对实际命令/目标清单及自身完整验收。协调者未替该PR批准、合并或改保护。

A01实现PR #2016还暴露现有路由未登记本次获批的A共享接线：`ci-plan.mjs::component()`将`src/hyprial/shell/impl/gui/adapter/host.py`及`tests/shell/gui/adapter/test_app_auth_host_wiring.py`归GUI，只允许intg/gui*，故在真正运行inventory之前拒绝A01。本地同head帮助清单实际PASS。A04的公共CI工作包须明确登记**准确A分支与必要公共Python消费者路径**并补正反例，不改分支名伪装GUI路线、不按PR号豁免、不允许src/gui/app/sidecar产品变更借A通行；保留比较基线、checkout身份、实际Python/契约检查及失败传播。此门在最终A容器解除WIP前必须关闭，不把它冒称当前已绿；A01不抢改公共CI。intg-workflow的容器验收规则用于记录这个跨工作包依赖，不能用整job同红掩盖新的产品回归。

## R4 复审输入与 A/B 边界

codex-sw 消息 ac7a2f82-d08d-5b1d-a16d-ec1360949afa 转交 reviewer 660ad02f 全文，绑定#2004 R4@3ac5381d0；原存档声明5034B、SHA-256 f585a3ec92faca13b30ef52e4b6e3dc8390c5a649258e0f8c94ec8bff61ca5ab。本线读到转交文本，未读取原文件重算哈希，不宣称复审放行。

- Q1：确认展示须含可信owner user身份、新URI的host段（旧文machine词汇）、harness版本、旧URI去向、按阶段判定的回滚条件/URI、预计停机窗口。缺项保持UNKNOWN及相应门未满足，不编造数值、不加兼容别名、不新增--yes授权层。
- Q2：A不拥有move单据、ACL/runtime状态。B集中提交单据计划/意向/执行引用的存储责任表，区别runtime恢复/停止收据和identity落地收据；ACL/runtime具名承接待B与组织/用户确认，并在对应实现前固定责任，不由A冒填。
- Q3：--replaces仍为创建专用建议；B补参数白名单、合法关联/旧终态及多新单竞争结果，同源全局围栏不依赖关联参数。后续schema未冻结不得先发占位能力。
- 其他LOW：摘要规范化/授权证明、同principal双角色授权、pending-verification/plan-conflict的取消恢复、check鉴权、persist-before-send、离线smolvm路径逐项由B收口。A公共基础获批不代替这些业务门。

## 启动前验证

- PAC format 2 plan：PASS，23节点/5个独立实现worker，开发节点均显式 pi/deepseek/deepseek-flash + --thinking max；A04 prepare/build复用同一worker。
- 当前dev基线结构门：FAIL，两条missing-product-root（src/gui、app/src），系#2009退役后结构根清单尚未同步；不能以恢复旧GUI或放宽7文件/1000行规则解决。A04公共CI工作包核对当前布局并修正对应清单/测试，后续验收必须消除此已知基线失败。
- 当前dev基线模块边界门：PASS，1056文件，未发现跨域违规；只是静态证明，不代替运行验证。
- 当前执行记录尚无产品变更，全套产品验证UNKNOWN，将由各任务和最终产物验收实跑。
- cc-kanban独立只读核验：5卡字段/四项清单/依赖正确，全板无同名重复（消息f6eca857-2225-5e4a-8801-4918d6687092）。

## PAC 已启动

- 汇总WIP PR：[\#2015](http://git.internal.example.com:3000/HyprialOS/harness-bridge/pulls/2015)，初始计划提交99552b700。
- 图：wf-19ff5a8b1e0140fb9a2d367241165ad9；operation-key=cli-command-surface-20261028-v1；发布结果running。
- A01请求已accepted，worker为agent:amy.williams@example.com:devusr-dev:wf-19ff5a8b1e0140fb9a2d367241165ad9-f1a8d12f9541，独立cwd=/Users/amyws/projects/cli-command-surface-a-01。
- agent list实查provider=deepseek、model=deepseek-flash、preferredHarness=pi、harnessArgs=[--thinking,max,--approve]、headless online；未用tier默认模型替代。
- 5卡均已链接#2015与PAC，5个OrgFS项目均关联同图；A01已doing，其余等待依赖。候选尚未提交，无验收项勾选。

## A01 accepted; A02 released

2026-10-28：A01 PR #2016 经 R1 退回修正后，R2 独立复审通过；fast-forward 合入 `intg/cli-command-surface@4ebcb5f98ec0a5811080450e384ad5a7b60f1fe9`。判词见 `docs/evidence/cli-command-surface/a01-review-r2-4ebcb5f98.md`。该 head 独立焦点 56 passed/1.57s、Ruff PASS；执行 AST 与已验证 ed85 相同（仅模块 docstring 修改）。全套原 FAIL 及各项 UNKNOWN 未注销，A04 新 CI_ROUTE/结构根门与 A05 最终产物复验仍开放。当前完成 1/5，容器保持 WIP。

A02 由既定 DeepSeek Flash Max worker 在最新合入基线上继续。公共注册/DI/output 必须提供真实接入点；无兼容 alias、测试专用重导出或新业务迁移。A02 后释放 A03 与 A04 清单准备并行；A04 正式接入仍等 A03。后续焦点测试先消除相关回归，全量固定源码后运行，适度使用 `-n 4 --dist=loadfile --max-worker-restart=0`，只以隔离子进程控制测试环境，不改宿主配置。

## 完整验证的执行分工更新

2026-10-28 A02 实测暴露后台 shell 启动的信号继承问题：按原 `nohup ... &` 方式启动的探针显示 SIGINT 被忽略；固定候选的同一信号用例在 SIG_IGN 下超时，在 SIG_DFL 下通过。证据见 `docs/evidence/cli-command-surface/a02-review-5c5bb85ca.md`。后续测试启动须在自有子进程中恢复 SIGINT 默认并记录实际探针，不改宿主信号或用户配置。

为避免 worker 和协调者重复启动全套，后续采用以下分工：DeepSeek Flash Max 仍负责所有产品/测试实现与返工，完成焦点、结构/边界、Ruff、适用契约验证并提交固定候选及 PR；协调者在独立检出/venv 上完成一次可追溯的全套验收。AGENTS 要求的全套检查仍必须完成，执行责任由协调者承接，不是免测。build 完成仅表示交审，accept 仍须等待独立验收与 PR 合入。未完成全套的 PR 如实写等待协调者验证，不写 PASS。

完整运行使用逐项日志、短 traceback、`faulthandler_timeout=120` 诊断、准确 SHA/PID/cwd/结束码；日志与 stdin 完全脱离工具管道。协调者运行期间独立检出保持冻结，worker 可整理文档和 PR；任何新的产品/测试差分必须报告，由协调者判断并实施相关重验。新的失败先核具体 nodeid/原因，保持原断言；重复全套仅因实质修正、未解决疑点或最终集成变化而启动。并行时总测试 worker 数不超过 8。

## A02 accepted; A03 and A04 preparation ready

A02 PR #2017 已 fast-forward 合入 `intg/cli-command-surface@d86c47cd20c961250c3c718537d789deae4b6b2c`，产品/tests 精确源为 `b702e5414a3e066cc164d19a5a51a5a21621698b`；两者在 src/tests/scripts/contract 零差分。独立判词见 `docs/evidence/cli-command-surface/a02-review-b702e5414.md`。协调者在逐个退回修复失效消费者、重复接线和测试耦合后，接受公共组合/DI/output 这一工作包范围；不把它称为完整发版通过。

独立完整测试自然结束：20 failed、15786 passed、17 skipped、8 xfailed、1 xpassed、1 error，1883.25 秒，退出 1。20 个失败均有原基线同因复现或环境对照，未留下已识别的 A02 新增回归；3 个末轮消费者修正和两个 SIGINT 用例在完整运行内通过。焦点 163 与 130 passed、contract rc0、Ruff 和模块边界 PASS。完整套件仍是 FAIL，原失败、两个缺失目录、CI_ROUTE 和 runner 网络问题保留在集成验收义务中；A04/A05 不得把它们默认为已关闭。当前完成两个工作包，容器仍 WIP，正式 B/C 公共产物交付仍等 A05。

后续遵循上一节的验证分工：DeepSeek worker 负责实现、焦点、结构/边界、Ruff、适用 contract；固定提交交审后由协调者独立跑一轮完整套件，不再双方重复启动。测试子进程除显式信号恢复外，还须隔离继承的 Git 环境：四个 warning-string 用例在原基线同样失败，仅去除子进程 `GIT_*` 和 `XDG_CONFIG_HOME` 后候选 4/4 通过。不得为通过这些用例改宿主配置、凭据或断言。完整套件慢尾的根因仍 UNKNOWN，不凭 native sample 结论改产品。

A03 负责 MethodSpec/真实绑定/OpenAPI/精确 import 棘轮和 link 同组样板，完整协议要求见 design.md §6–8。A04 准备阶段与它并行，只修改实际清单、消费者核对、负控和公共 CI 准备；最终绑定须等待 A03 已验收版本。A03 不抢改 A04 的 `.forgejo/workflows`/ci-plan；A04 不造假 schema 或提前注册 B 的 move。双方所有共享文件交叉先向协调者报告，串行合入。

A04须同时处理精确的 A 公共 Python 消费者路由（包括 A01 adapter host 与 A02 lifecycle 测试）、退休根和失效 workflow test targets；新增/未来 src/gui/app/sidecar 产品路径仍不得借 A 放行。#1997 在本次复核时仍 open、dev 仍 9d0f5a5e7，开工再次核对；已交付成果优先引用，不重复做平行补丁。精确 CI 来源见 A02 判词，正文编辑的绿灯不能替代 synchronized 源码事件。

实际释放回读：a03-build 请求 `workflow-request:acc29f7ad450468d8f8911caf7d1d48f`、a04-prepare 请求 `workflow-request:d250a0c87dbb45e4966230cf59910779` 均 delivery accepted；agent list 核验两个独立 cwd 中均为 pi / deepseek / deepseek-flash，harnessArgs=`--thinking max --approve`，status=online。对应 Kanban 与 OrgFS 均 doing、验收 0/4；A02 已 done 4/4，未选 reject 旗标保持 false。


## A03 accepted; A04 final build released

A03 #2020 已 fast-forward 合入 `20269c960c98ba5def5abe65d3948a60ea8f8550`；独立 scope PASS 判词为 `docs/evidence/cli-command-surface/a03-review-2d217e207.md`。源代码固定 2d217e207，测试仅增加清单四行与短 socket 路径两项修复。独立 85 焦点、224 清单消费者、link 串行9/并行9、43相关复验及负控通过；52cf 完整套件仍 FAIL（33 failed/15844 passed/3 errors），11项修复已验证，16项旧失败与6项全量专有 UNKNOWN 留最终集成门，不放宽断言。当前3/5，容器WIP。Kanban及OrgFS A03均4/4 done。

PAC a03-review请求3b72f36539f94a20af0a44607190bf7e与a03-accept请求5a4cdfb8292d4f7794aa08bdfa8f786a已完成；未选reject不置位。A04在原卡/#2018继续真实A03接入，不新建重复卡。首次ledger审核seed绑定上述已合SHA：import ledger SHA256 `5b220a29cb1ad95f95d8b63ec2aafb746320f28287c90fd19e334236075eb4e5`，legacy-method ledger SHA256 `46357f8d4da53bc13b122c3717de8b7fe0c3db1ba5f236ea5d1f970473ef10ae`；195 unique observed含4 registered，并非199。后续PR比较真实base，不接受永久缺base回退。A05最终同步最新dev（本次只读5f6e4d0ed）后验证安装产物；A03不会冒称交付完整B/C消费基线。


## A04 accepted and A05 released — 2026-10-28 09:2x CST

PR #2018 fast-forward merged `45a7d613577d022c5cb04412940caf9cb6549b11`.
Coordinator final scope verdict `0042856165f8af870e79d902daf5c817325401d4`,
`docs/evidence/cli-command-surface/a04-build-review-5e25bbf0a.md`:
279 focused tests,40 real depth-one architecture tests,three actual YAML fetch
and complete-gate entry points,inventory edit/deletion controls and Ruff/structure/
boundaries PASS. All A04-owned defects closed. Kanban/OrgFS4/4done; total4/5.
Fullb919 remains17failed15898passed2errors;15baseline and2OrgFS full-only UNKNOWN
failures remain final obligations. Remote ec632 request-contract step actually
passes, but pytest jobs did not run after upstream runner network failures.
No CI-wide PASS or release readiness is claimed.

Latest dev `5f6e4d0ed246ecd1be19f9df801eb94e1d98cfaa` was merged without conflicts
as `d8a7fa88cda05dec112f19a15f05b103fd5dfc72`. This imports its four existing
GUI guide/port-reuse files only; the coordinator wrote no product/test code.
A05 must verify this combined tree, including the affected launcher tests.
The container stays WIP pending installed artifacts/final gates/independent QA.

A05 execution supplement (same authorized scope):

- Use an actual non-editable wheel, installed outside every source checkout.
  Clear source PYTHONPATH in child environments, print distribution/import paths
  and artifact SHA-256. Do not run the repository OpenAPI helper that inserts
  ROOT/src as evidence of an installed public interface.
- Verify installed public kernel client/declarations and daemon catalogue/OpenAPI
  entrypoints; real isolated short-path daemon, CLI text/JSON and error behavior.
  Own only the child PIDs and temporary state; no production credentials/state.
- Standard published wheel includes the Go sidecar. A worker-mode thin wheel is
  a distinct existing artifact mode, never proof of the full platform wheel;
  do not enable the missing-sidecar escape hatch to turn a build into PASS.
- Investigate the named gitless thin-wheel test with the actual ZIP entries;
  source inspection suggests substring `_bin` also matches ordinary binding.py
  names. If confirmed, fix the precise path predicate with real payload negative
  controls in this task, without weakening the payload requirement.
- Coordinator owns independent full-suite validation on the frozen final source;
  worker runs focused/installed/contract/lint/boundary checks, avoiding redundant
  full runs. No unrelated domain business changes to conceal baseline failures.
  Report any new regression with exact ID/trace and ownership for return.
- Final source plus artifact must include latest dev. Record immutable interface
  snapshots and B/C consumer instructions. All acceptance remains evidence-based;
  an installed proof alone does not close known final-suite/remote CI failures.

## A05 interrupted; bounded rework remains (2026-10-28)

A05 candidate `7ef518645d28357509e8baf853e09d71838bf245` is NOT ACCEPTED.
Independent review and exact accepted/candidate file manifest are in
`docs/evidence/cli-command-surface/a05-preflight-review-aed9.md` and
`docs/evidence/cli-command-surface/file-manifest-20261028.md`.
27 focused tests and lint/structural/boundary checks pass; the existing cb7
standard wheel separately passes1138 member comparisons and13 real isolated
CLI cases. These results do not close the three concrete review findings.

When the original A05 worker is recovered under a fresh authorized PAC
request, preserve its uncommitted evidence and continue the same task/model:

1. Correct early daemon exit reporting/log lifetime, default console resolution
   within a uv venv, and the omitted public IdempotencySpec installed probe.
   Add meaningful regression tests and demonstrate red-to-green behavior.
2. Refresh consumer documentation and artifact coordinates, explicitly naming
   source SHA, validator SHA and merged dev SHA; current dev is5f6e4d0ed,
   d8a7fa88c is its integration merge, not a dev commit.
3. Submit a frozen A05 PR to intg/cli-command-surface with validation and the
   existing unresolved full-suite/remote-CI facts. Do not implement unrelated
   domain changes to hide baseline failures or mark acceptance yourself.
4. Coordinator independently reviews repaired branches, installed final artifact
   and final integration/full gates; only then complete the A05 acceptance card
   and PAC nodes. Container stays WIP until its required gates and QA are met.

The original graph is held due provider Connection error; retries exhausted.
A coordinator-identity restart returned WORKFLOW_NOT_OWNER. Recovery belongs
to the graph creator through the supported workflow worker restart action,
with h2b-monitor diagnosis requested; no replacement graph or identity/model
fallback is authorized by this interruption. A01–A04 completion is retained.

## A05 recovery confirmed — 2026-10-28

The user reports executing the supported workflow restart with h2b-monitor.
Coordinator inspection at epoch1793155964114 confirms the original graph is
running and a05-build has fresh request
`workflow-request:8805abc32c2349debde0fa3f53994270`, state requested,
delivery accepted. The original worker remains pi/deepseek/deepseek-flash with
`--thinking max --approve`; A01–A04 accepted flags are preserved.

Recovery feedback `0356a474-10d3-53a9-b518-d7c98a74bfa5` points the worker to
the three independently reproduced findings and this bounded handoff. Its
existing checkout and uncommitted JSON evidence are preserved. The previous
provider interruption is no longer a scheduling blocker; execution progress,
repairs and final artifact/full-suite acceptance still require fresh evidence.
A05 remains doing, not accepted; #2015 remains WIP.

## A05 final boundary rework and validation scope — 2026-10-28

The user requests rapid completion without repeated validation. The fixed
review is [a05-review-b298](../../evidence/cli-command-surface/a05-review-b298.md)
at `3bdaaf2916f80f7beb35b57f778f3a2fc0472166`: installed acceptance and the
consumer fixes passed independent checks; import-boundary rework remains.
Only reject was selected. Current A05 build request is
`workflow-request:a82f0c02ac3344568e3b4041417b7acc`, inspected as requested with
delivery accepted. The original worker continues; A05 is not accepted.

Implement bounded fixes, checking each actual diff. Reuse unchanged evidence;
do not repeat broad focused or installed runs for documentation-only changes.
After product/gate freeze, rebuild and check the final artifact once, and the
coordinator runs the required final full suite. New failures still require
investigation; urgency does not turn an unrun or failed gate into PASS.

Post-install warmup belongs to the explicit shell bootstrap capability (B05),
with the real replacement/import fence preserved. Online doctor diagnostics
must use request; only explicit daemon unavailability permits read-only B06
diagnostics. Returning classes or forwarding `worker_binding` through a new
helper solely to hide imports is not a boundary fix. Reasoning-effort encoding
and credential loading are behavior, not protocol vocabulary or blanket
offline exemptions. Their owning boundaries must be explicit.

The original accepted A03 commit, seed and digest anchors remain immutable.
A real migration may shrink the current ledger; first-seed handling must then
prove it is a subset of that immutable accepted ledger, never replace the
anchor or permit new/increased entries. Any such gate change requires focused
positive and tamper/growth negative controls. No blanket 42-entry exception
or the worker's proposed eight-entry manifest has been approved.

## GUI and agent-move consumption sequence

The user's proposed sequence is the intended consumer workflow: map user
journeys and functions to existing methods, wire available capabilities,
then implement missing backend contracts/handlers incrementally. Record
authorization, confirmation, errors and accepted-versus-completed behavior
per journey; do not represent an unimplemented method as an available action.

A provides the shared CLI request/client/composition foundation, MethodSpec,
OpenAPI export and real link samples; it does not migrate every CLI command.
Current GUI dev mode explicitly uses a Node placeholder backend returning
HTTP 501 (`gui/README.md` and `shell/impl/gui/launcher.py` in dev). A real GUI
bridge with trusted caller identity is still needed. It should consume the
same daemon methods, not CLI text output or direct business stores. GUI and
agent-move consumers can align journeys now; A acceptance remains the code
baseline prerequisite and does not imply their missing business methods exist.
