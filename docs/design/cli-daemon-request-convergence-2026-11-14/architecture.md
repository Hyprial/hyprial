# CLI → daemon request 收敛架构建议（WIP，待owner确认）

基线固定为 Forgejo dev `533ef27ca42e53230224d55e58ba074f93d0eb15`；#2727、#2755未合并，不计入完成项。本文件只提出设计和静态清单，不授权产品改动。旧设计中195 IPC、231命令、548 import等均是历史数字，不作为当前覆盖率。

## 1. 盘点结论与计数口径

- `scripts/ci/legacy-method-baseline.json`确有175方法名、7前缀。
- 在固定源码上加载纯`METHOD_REGISTRY`得到138个真实绑定；175账本中87已有同名声明，88尚未声明；另51真实绑定不在175旧账本。
- 现有method checker静态collect观察226名字、7前缀、0 unsupported。226正好是175旧账本与138 registry的并集；这不是226条仍运行的legacy branch。
- 暂无足够证据批准88中任何完整方法直接删除，亦无完整异名方法已证等价替代。建议87复用、88先按现有操作补声明；后续若收敛异名方法，逐条补消费者全量与语义等价证据后owner裁定，不能提前扣数。
- [methods.json](methods.json)逐条保留175名的当前分派表达式/行号、87已声明handler/owner/metadata、88建议owner/风险，以及完整138声明集合；[methods.md](methods.md)为可审表格。静态检查不是参数/结果/错误/权限/side effect的运行时验收。

## 2. 不能直接替代的具体证据

| 现有操作 | 近似声明 | 缺失语义与证据 |
| --- | --- | --- |
| `down` | `agent.stop` | `request_catalog/agent/lifecycle.py:756`固定all=false、agentsOnly=true；`application/actors/lifecycle_ops.py:896`的down还覆盖all/provider/adapter与消失spec后的binding清理。CLI不得自行foreach模拟批量业务；保留完整daemon down或由owner批准daemon批量命令。 |
| `ping`、`ps` | `daemon.status` | `request_catalog/maintenance/status.py:69`只投影running/version/phase；`ipc/router.py:324`ping在restore gate前且包含身份/网络/forwarding等信息；`application/messaging/status/views.py:84`ps含connector/queue/agent等全量状态。不能删掉旧字段或改变恢复期可达性。 |
| `lifecycle.start`等3方法 | `agent.start` | `request_catalog/agent/lifecycle.py`文件头已明确fresh headless范围；交互、resume、replaceInteractive、smolvm不在同一参数域。必须追踪全部入口而不是因存在agent.start便判等价。 |
| `management.squire.ensure` | `management.squire.prepare` | `request_catalog/setup/squire_specs.py:1`明确prepare含配置/启动编排，不是ensure别名；prepare虽然有SPEC源码，当前真实registry未绑定，不能宣称现成能力。 |
| `identity.whoami` | `identity.resolve` | `application/messaging/status/views.py:527`返回sessionRegistered/daemonEpoch等，会回答stale child身份而不先fence；名称相近不等价。 |
| `agent.destroy.preview`/destroy | 新确认协议 | `agents_admin/registry_ops.py:808`preview当前只回actor/agent/workspace，`:825`execute仍按name执行。引入operationId是有意契约变化，应单独批准与RED/GREEN，不可写成零行为变化。 |

## 3. 唯一业务边界

CLI只负责argv/参数字面解析、TTY/确认、显式IPC调用、renderer和统一输出。对象存在性、配置合并、授权、provider选择、状态存储、跨步骤调度、重试/补偿策略由daemon请求handler及其唯一领域服务负责。daemon装配可消费identity/kernel公共能力；daemon不反向import CLI或biz/shell impl。需要上层领域服务时由既有port装配，CLI不直接调用该服务。

kernel只保留IPC transport、协议词汇与纯数据类型；将业务helper搬到kernel、shell公共导出或新shared目录不会使其合规。也不能将CLI本地业务包成一个函数、放进例外清单便宣布迁移。CLI→HTTP不可作为本批出口；已有OpenAPI只是同源契约投影。声明的httpExposed无需为CLI迁移扩大。

`CliRuntime.request`是CLI通用request port；现有`support._daemon_request`中的session binding/endpoint选择属于传输与可信调用者装配，但必须拆清与混杂业务，不能把整个support模块当合法白名单。CLI输出继续经CliResult/CliStream；RPC envelope与CLI `{ok,...}`不可混为一个协议。

## 4. MethodSpec迁移时必须保留的边界

逐方法记录参数类型/必填/默认/未知字段和旧coercion、合法producer输出全部可选字段、所有公开错误code/data、可信caller来源、mutates/retry、取消与cursor、恢复期准入、确认协议。声明绑定真实handler，不能空handler或统一放宽schema过门。旧方法保留`ipc_param_validation`/legacy参数顺序的必要性须具名审查；不能通过新严格schema不经审批改变旧输入域。

- routine剩余6方法：`ipc/router.py:516`先调用_workflow_caller，再为remove/pause/resume/set检查owner/actor；registry在此前返回，迁移时必须把这个共同授权路径移入daemon handler/preauthorize，不能漏掉或复制两份。
- identity binding：`ipc/router.py:467-491`按trusted worker、IPC peer和interactive绑定计算operator/peer_pid；业务params不可自报权限。
- session.register/guarded：`ipc/router.py:496`的verify_process_claim由可信origin控制；worker.channel.request的generation/session fence与origin=worker递归分派保持。
- service4方法在`ipc/router.py:396`，位于inbox/harness/presence assertions之前；ping在restore gate之前，shutdown是light method。统一registry时不能改变这些恢复/停机准入时序。
- message read_body会写fetched，pending wait是长轮询，workflow status/history可有远端观察；名称不能替代副作用核验。
- 已声明87不自动代表CLI已干净；完整命令可能仍读取store或执行本地编排。也不代表应删除统计表/策略比较里的同名常量。只删确认不可达的分派分支。

## 5. 例外建议（逐项待owner确认，不继承历史整组许可）

| 候选类别 | 精确能力边界 | 明确不涵盖 |
| --- | --- | --- |
| 首次bootstrap | 创建最小home、建立启动daemon必需身份材料、拉起OS服务；已有daemon时后续onboarding/auth/org/agent配置使用已声明请求 | init/login全组、本地org membership/provider配置、在线失败自动fallback |
| daemon生命周期 | run装配、start/stop/restart的OS service manager协调、exit proof；有socket时shutdown仍request | 状态查询本地store fallback、CLI内业务恢复/调度决策 |
| 安装/升级 | 安装器替换、安装锁、必要preload、OS服务启停与回滚，现有autoupdate.status/upgrade.steps.*走IPC | 在线scheduler notify/trigger业务或普通配置写入 |
| profile/version | 启动前home/profile发现与显式选择、安装版本来源读取 | 第二个全局业务selector、联网升级业务或任意home store操作 |
| 离线诊断 | 仅明确daemon不可用时的只读诊断，精确字段/来源列出 | 在线AgentRegistry读、修复/写入、凭据内容泄漏 |
| 离线维护 | adapter downgrade-state 保持停机、ownership fence 与公开 refusal（no preview/commit）；org repin 保持停机、fence、preview/commit；各自条件分别由 owner 审核 | “maintenance”整组、在线配置操作、无法证明停机时执行 |
| 终端前端 | TTY、terminal attach、用户选择的本地编辑器/浏览器打开；业务launch plan和授权由daemon返回 | CLI推导provider/secret/launch策略或直接写desired state |
| 纯前端/协议 | help/明确占位错误/格式化，具名协议错误常量和不可变payload类型 | 以纯函数/类型名掩饰读取home或业务决策 |

每条例外必须具有command path、源码symbol、公开能力、运行前提、允许的I/O/副作用、为何无法经daemon、验证负控、owner与复审/退出条件。不要为降低import数移动导入或借动态import逃扫描。历史B01-B09、A05八项和当前manifest只是审查输入，不是本次无条件批准。

## 6. 实施闸门与共享文件顺序

先提交本轮docs/design WIP，owner确认目标、精确例外、确认协议及批次后才实施。每批以真实方法+完整CLI调用链为单位；共享registry/migration/router串行集成，后合批更新到准确dev并重新生成OpenAPI。禁止碰gui、shell/impl/gui、contract_mock、clients、packaging；若CLI最终搬目录必须修改packaging，记录为独立owner依赖，不在本批强搬。

验证包括纯registry/contract导出、CLI参数/JSON/text/exit对照、错误data与授权负控、真实隔离AF_UNIX调用、仅批准的例外负控、import/method/architecture ratchet不增长、结构/模块边界。实施时按当前AGENTS执行src/hyprial变更后的最终全量；设计文档不冒跑全量充数。基线问题如KP-038网络门、KP-041 org.join错误数据应单列，不借本次隐式放宽；BLOCKED/UNKNOWN不可写PASS。网络门须跟踪registry handler可达性，不能只把router旧分支删完即认验收。
