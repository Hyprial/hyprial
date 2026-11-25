# Coordinator：分支、并行调度、审核与合入

负责人：**linyilun**。统筹执行角色：**emergent**。
开发者：各线自行承担；统一代码审核入口：**codex-daemon-request**（yaoshengyue）。
先读 [总架构](00-architecture.md)。公共线由 yaoshengyue / codex-daemon-request 承接，其作者代码另须非作者独立审核。

## 1. 容器与首个工作包

从最新核验的 `release/v0.5.5.4` 建 `intg/gui-live-0554`，用最小启动提交建立 WIP 容器 PR：head=`intg/gui-live-0554`，base=`release/v0.5.5.4`。完整七文档规划作为首个工作包 PR 合入容器；随后各线从 coordinator 公布的容器 SHA 建分支。

本轮已建立的容器为 [PR #2401](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/2401)，起点是总架构固定的 `764cf168ecf39713da463af66557cda0044b1fee`。接手时先核真实 head/base 与工作包状态，不能把创建容器当作实现完成。

建 PR 的操作人不改变责任分工：容器由 emergent 统筹维护、接收审核并管理 WIP/ready 状态。建容器不代表批准实现成果；功能线不得自行向 release/dev 提交或移除容器 WIP。

## 2. 并行任务与依赖

产品分四条功能线，公共线负责共享底座。公共文件 owner 固定为 yaoshengyue / codex-daemon-request；coordinator 为 linyilun / emergent。各线可同时开发，表中的依赖是接口和最终放行依赖，不是等待其他线全部完成才开工。

| 任务 | owner 角色 | 依赖 | 交付/放行条件 |
|---|---|---|---|
| P0a 共同契约与冷启动设计落实 | yaoshengyue / codex-daemon-request；非作者独立审核 | 本设计审查 | 先做公共线文档规定的结构抽取，再落实冷启动/身份/协议章节中的锁/阶段/HTTP 重启/IPC capability/身份隔离/Mock journey；固定方法/schema、T11 扩展与错误/unknown 测试；不是只补 OpenAPI |
| P0b 公共 HTTP/GUI client/宿主 | yaoshengyue / codex-daemon-request | P0a 接口冻结；可先用显式测试替身开发 | 一个运行时 driver、一个 client factory、一套同源会话；Node/wheel 公共协议；现有业务 HTTP 无回归 |
| U1 GUI 首次旅程 | huangjiajia | P0a+b | 空隔离 home 从 GUI 登录/注册入口到身份/设备/初始化/模型/管理 agent，实际回执，不要求用户先运行 login/init |
| U2 后续 settings | huangjiajia（本线契约），公共线协助核心接入 | P0b；按每个方法是否已有契约 | 可与 O/A 并行；无方法的页明确 unavailable，不阻塞已就绪的 org/agent |
| O 组织接线 | yaoshengyue（org） | P0b；live 验收依赖 U1 或已初始化隔离 home | 普通 App 注入源；同组件 live/contract-mock；无 fixture fallback |
| A agent 接线 | linyilun（agent） | P0b；U1 的身份与 provider 准备 | 现有 generated 方法统一注入；与 U1 不重复实现 create/start/credential |
| C0 chat 缺口契约 | lizhenyu（业务 Spec/handler），公共线维护核心 | P0a 的身份/receipt 规则 | message.send/targets/progress/pins 等必要契约及负例；逐项判断，不虚构 handler |
| C1 chat GUI 迁移 | lizhenyu（chat） | P0b+C0；U1/A readiness | 发送、回执、刷新恢复、unknown 不重复、Lark 权限与 CAS；主 chat/agent-chat 共用 |
| I1 组合验收 | emergent 组织；codex-daemon-request 审核 | U1、O、A、C1 | 同一入口完整旅程；错误路径；Contract Mock 模式；安装态无需 Node |
| I2 清理/发布 | emergent 统筹；发布按既有授权 | I1 实测通过 | 移除产品旧业务桥；按下述 release→dev 流程；不可将局部 PASS 当产品全通过 |

前置关系是“可信身份/初始化 + 公共 client”，不是“user-setting 所有诊断、升级、绑定页面完成后其他线才能干活”。已初始化的隔离 home 可提前做 O/A/C live 开发，但不能替代 U1 冷启动验收。

## 3. 合并路径

从开工时最新 `release/v0.5.5.4` 建一个新的 `intg/gui-live-0554` 容器：

```text
release/v0.5.5.4@固定基线
  → intg/gui-live-0554
     ← P0a / P0b / U1 / U2 / O / A / C0 / C1 各自短分支 PR
     ← 组合修复与生成物重生成 PR
  → 去 WIP、固定整候选、独立 review + CI + 浏览器/安装态证据
  → PR: intg/gui-live-0554 → release/v0.5.5.4
  → PR: release/v0.5.5.4 → dev（emergent 按既有权限统筹）
  → 版本号/构建产物/签名与发布门（独立发布授权）
```

不把 feature 包装成 hotfix 直进 dev。旧 intg/gui-v1 的祖先关系不能替代新容器的验收。不得为了让本补齐仍叫 0.5.5.4 重写已发布 tag；若实施时该版本已正式发布，则由发布 owner 确认后续修订版本，代码目标/测试证据据实更新。

现有 `.forgejo/workflows/gui.yml` 的 PR base 触发覆盖 intg/** 和 release/**，push 覆盖 intg/gui**；因此不要说“所有 feature head 必须叫 intg/gui 才运行 GUI CI”。选 intg/gui-live-0554 是容器推送验证路径。source-branch 对 dev 只接受 release/v* 或 hotfix/*，见 `scripts/ci/check_pr_hygiene.py`。本方案不改 CI workflow，不绕保护。

## 4. 固定的审核与合入操作

2026-11-04 用户接受优先接线上线和测试分级；下述审核/合入按
[阶段放行调整](integration-gates.md) 的 M1/M2 与适用硬门执行。
公共首包使用 [接口 v1](common-interface-v1.md)。接口合入后由 emergent 广播各负责 agent
并在飞书群通知接口 ready，附合入 SHA/文档与运行时未覆盖项。

1. 维护容器 PR 的工作包表：负责人、分支/PR、精确 head/base、依赖接口版本、共享文件、测试、审核结论、阻塞项。规划文档是首个工作包，不与公共线产品实现混成一个 PR。
2. 四线和公共线均可同时开工。各线只在自身目录/方法子包开发，共享文件登记给公共线；同一人的 coordinator/agent 或公共/org 职责使用不同工作包和分支。
3. 开发者提交固定 head 给 codex-daemon-request 审核；报告必须区分实现/契约/CI/真实浏览器范围。REWORK 交原作者，改后提交新 SHA，旧失败证据不删除。公共线由非作者先独立审查，不能自己给自己 PASS。
4. 只将审核通过且依赖已就绪的 head 纳入合并队列。emergent 按最新容器 SHA 调度合入；不绕保护、不 force merge、不替作者静默解决业务冲突。linyilun 的 agent PR 同样先交审核。
5. 每合一项，待合作者把新 base merge-forward 到自己的分支并重新核验。记录冲突处理；生成物由公共线在组合 head 重生成。基线失败按具体 test/断言归因，不能只说某 job 原来就红。
6. PR CI 与 intg push CI 分开记录 branch+SHA+event。并发 push 可取消旧 run，最后一次合入后必须在整候选 head 重跑组合门，不能引用上一个 head 的绿色。
7. 按阶段放行调整核对整候选，明确本次是 M1 源码预览还是 M2 完整交付；不得把 M1 写成冷启动/wheel 全通过。适用硬门通过、已知问题有 owner/后续修复路径后，交维护者按 intg→release→dev 接收。目标更新需先 merge-forward 并重新审核组合 head；未结任务保持 OPEN，发布另走版本/产物/签名门。
8. 容器若停止，显式关闭并记录已合/未合/原因，不留永久 WIP。不得静默改目标、重写已发布 tag 或把 feature 包装成 hotfix 直进 dev。

PR 正文首行标真实 `Submitted-by: agent:<提交者 URI>`，并写 Skill-Impact、Contract-Impact（或同步契约）、动机、具体改动、精确测试与未覆盖项。目录统一官方 worktree 路径，Git transport 用内网 SSH 地址 `ssh://git@<内网 Git 主机>/HyprialOS/harness-bridge.git`；不拷贝凭据。

## 5. 开发与统筹 prompt

> 你是 coordinator/emergent，负责人 linyilun。维护 intg/gui-live-0554，基于固定 release/v0.5.5.4，容器 PR 目标 release/v0.5.5.4，保持 WIP。先接收七文档规划工作包，再让公共线 yaoshengyue/codex-daemon-request、agent linyilun、user-setting huangjiajia、org yaoshengyue、chat lizhenyu 并行开工；各线独立短分支和 PR，统一 base=intg/gui-live-0554。公共线先给稳定接口坐标；feature 可先做 adapter/业务契约/测试，不得在等待期间私造 transport/fixture 入口。所有实现固定 SHA 交 codex-daemon-request 审核，公共线作者代码另经非作者审查。审核通过与依赖就绪后由你统筹合入，自己的 agent 线也不能免审核。管理共享文件和生成物归属、合入后的 sibling base-freshness、冲突/测试回归；CI 按 SHA+event 记，不引用旧绿。最终在同一个整候选证明从 GUI 登录/注册开始所有后端业务走统一 MethodSpec/FastAPI/daemon.request 链，唯一 driver 切 Contract Mock，缺能力返回明确错误，源码和无 Node 的 wheel 均可使用。整候选验收后走 intg→release/v0.5.5.4→dev；不自行扩大范围、不绕保护、不把设计/局部 PASS 当产品发布。
