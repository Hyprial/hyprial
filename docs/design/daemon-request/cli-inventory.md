# CLI 逐项清单（设计附录）

基线：`4a51775186e3e6087bde7c5826980d195333ed65`。主设计见 [design.md](design.md)。

2026-10-28：本表记录旧基线事实与当时的迁移提案，**不是 agent move 最终命令树**。
`agent move`、`agent host-invite`、隐藏 transfer 入口及其拟新增方法须按[跨线协调记录](coordination-2026-10-28.md)
与 [A 提案的 R4 活动命令表](cli-surface.md) 对齐；用户已选择不可变单/八动作，
本表的旧 `transfer.*` 拟议名不再是 move 目标契约，不能据此批量固化旧命名。
本表 DR 编号仅保留首版任务索引；新的 A/B/C 归属与审批流程见 [A 提案](cli-surface.md)。

本表来自在临时空 home/state 下加载**该基线源码**后的完整 Typer 注册树；仅枚举注册信息，
没有执行命令业务、连接 daemon 或访问生产配置。解释器为已安装 hyprial 的 Python 3.12，
Typer 0.27.2；不是本工作树锁定依赖测试环境。锁定依赖安装的限制见主 PR 验证记录。

共 **223 个叶子 + 4 个可直接执行的组 callback = 227 个入口**；25 个源码命令族，
其中 GUI 1 个入口标 D，其余 24 族进入任务。H 表示隐藏（包含父组隐藏），G 表示组 callback。
普通分组的 help/注册 callback 不重复算业务命令；root 参数解析由 DR-30/31 覆盖。

分类：I 已有 IPC；N 需要新增 IPC/扩展现有方法接走本地业务；I+N 混合；
B 引导白名单（只开放设计 §5 的能力）；P 纯前端展示/占位；D 本批不迁。
I 不表示迁移已经完成，仍须去掉间接业务调用和旧导入。全部新方法名是**待审提案**。

“已有 IPC 线索”由 callback、局部 helper 和可解析 CLI helper 调用中的方法字面量/常量取得，
包括可选分支，不保证每次执行都调用。不能借该列断言运行时调用顺序、频率或业务纯度。
通过领域模块回调动态调用的方法可能不在该列，完整现有方法集合见 [IPC 清单](ipc-inventory.md)。
来源链接指向基线 callback；分析必须同时追踪它使用的 shared helper。

## 命令族覆盖

| 源码族 | 入口数 | 任务 |
| --- | ---: | --- |
| `adapter` | 21 | DR-19 |
| `agent` | 31 | DR-18 |
| `config` | 1 | DR-11 |
| `daemon` | 9 | DR-21 |
| `doctor` | 1 | DR-24 |
| `fs` | 27 | DR-14 |
| `gui` | 1 | 另线 |
| `host` | 3 | DR-07 |
| `init` | 3 | DR-22 |
| `link` | 4 | DR-05 |
| `log` | 1 | DR-10 |
| `login` | 2 | DR-23 |
| `message` | 12 | DR-13 |
| `meta` | 2 | DR-09 |
| `org` | 19 | DR-27 |
| `profile` | 4 | DR-12 |
| `routine` | 10 | DR-17 |
| `service` | 9 | DR-06 |
| `socialware` | 1 | DR-26 |
| `squire` | 3 | DR-20 |
| `transfer` | 19 | DR-28 |
| `upgrade` | 4 | DR-25 |
| `user` | 9 | DR-08 |
| `work` | 13 | DR-15 |
| `workflow` | 18 | DR-16 |

## adapter

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `adapter add` | — | I+N | `adapter.reload` | 拟 adapter.add；配置校验/注册/重载编排归 handler | [adapter_add](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L362) |
| `adapter remove` | — | I+N | `adapter.status`, `adapter.stop`, `management.adapter.remove` | 拟 management.adapter.remove；沿用方法并吸收停止检查/删除编排，增加配对 preview | [adapter_remove](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L488) |
| `adapter start` | — | I | `adapter.start` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_start](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L548) |
| `adapter stop` | — | I | `adapter.stop` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_stop](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L562) |
| `adapter status` | — | I | `adapter.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_status](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L575) |
| `adapter reload` | — | I | `adapter.reload` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_reload](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L589) |
| `adapter list` | — | I | `adapter.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_list](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L599) |
| `adapter pin` | — | I | `adapter.pin` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_pin](../../../src/hyprial/shell/impl/cli/commands/adapter/pins.py#L10) |
| `adapter unpin` | — | I | `adapter.unpin` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [adapter_unpin](../../../src/hyprial/shell/impl/cli/commands/adapter/pins.py#L36) |
| `adapter route list` | — | N | — | 拟 adapter.route.list；读取路由配置移到后端 | [adapter_route_list](../../../src/hyprial/shell/impl/cli/commands/adapter/routes.py#L83) |
| `adapter route candidates` | — | N | — | 拟 adapter.route.candidates；候选选择与配置读取移到后端 | [adapter_route_candidates](../../../src/hyprial/shell/impl/cli/commands/adapter/routes.py#L106) |
| `adapter route add` | — | I+N | `adapter.reload` | 拟 adapter.route.add；配置写入和 reload 由后端编排 | [adapter_route_add](../../../src/hyprial/shell/impl/cli/commands/adapter/routes.py#L131) |
| `adapter route remove` | — | I+N | `adapter.reload` | 拟 adapter.route.remove；配置删除和 reload 由后端编排 | [adapter_route_remove](../../../src/hyprial/shell/impl/cli/commands/adapter/routes.py#L171) |
| `adapter identities sync` | — | N | — | 拟 adapter.identities.sync；同步与本地记录归后端 | [adapter_identities_sync](../../../src/hyprial/shell/impl/cli/commands/adapter/data.py#L57) |
| `adapter identities list` | — | N | — | 拟 adapter.identities.list；读取身份记录归后端 | [adapter_identities_list](../../../src/hyprial/shell/impl/cli/commands/adapter/data.py#L94) |
| `adapter identities find` | — | N | — | 拟 adapter.identities.find；身份检索归后端 | [adapter_identities_find](../../../src/hyprial/shell/impl/cli/commands/adapter/data.py#L120) |
| `adapter media get` | — | N | — | 拟 adapter.media.get；媒体检索/下载归后端；CLI 仅处理输出目标 | [adapter_media_get](../../../src/hyprial/shell/impl/cli/commands/adapter/data.py#L163) |
| `adapter scopes check` | — | N | — | 拟 adapter.scopes.check；远端授权范围检查归后端 | [adapter_doctor](../../../src/hyprial/shell/impl/cli/commands/adapter/admin.py#L609) |
| `adapter scopes authorize` | — | N | — | 拟 `adapter.scopes.authorize.begin`、`adapter.scopes.authorize.poll`；授权状态由后端持有，CLI 仅展示/收集交互 | [adapter_authorize](../../../src/hyprial/shell/impl/cli/commands/adapter/authorize.py#L137) |
| `adapter user-auth check` | — | N | — | 拟 adapter.user-auth.check；状态/计时器和路由检查归后端 | [lark_auth_check](../../../src/hyprial/shell/impl/cli/commands/adapter/lark_auth.py#L110) |
| `adapter user-auth complete` | — | N | — | 拟 adapter.user-auth.complete；授权结果提交与通知归后端 | [lark_auth_complete](../../../src/hyprial/shell/impl/cli/commands/adapter/lark_auth.py#L186) |

## agent

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `agent create` | — | I | `agent.create`, `agent.provider.login` | 已有 RPC；将默认 provider 选择等业务下沉到同一后端操作 | [agent_create](../../../src/hyprial/shell/impl/cli/commands/agent/create.py#L17) |
| `agent host-invite` | — | I | `agent.host-invite` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_host_invite](../../../src/hyprial/shell/impl/cli/commands/agent/create.py#L106) |
| `agent list` | — | I | `agent.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_list](../../../src/hyprial/shell/impl/cli/commands/agent/admin.py#L41) |
| `agent readiness` | — | I | `agent.readiness` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_readiness](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L30) |
| `agent ensure-home` | — | I | `agent.ensure-home`, `agent.home-census`, `agent.list` | 保留 ensure-home/census/list 的语义，业务选择在后端完成 | [agent_ensure_home](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L126) |
| `agent start` | — | I+N | `agent.create`, `agent.get`, `agent.resume.restore`, `agent.runtime-context`, `agent.runtime-launch.acquire`, `agent.runtime-launch.release`, `agent.start.preflight`, `dispatch.matrix.resolve`, `lifecycle.resume.preflight`, `lifecycle.start`, `lifecycle.start-smolvm`, `ps`, `session.register`, `session.register_guarded`, `session.unregister` | 拟 `agent.start`、`agent.terminal.read`、`agent.terminal.write`、`agent.terminal.resize`、`agent.terminal.close`；封装当前启动编排；复用 lifecycle/session 逻辑；附件会话绑定，见设计 §8 | [start](../../../src/hyprial/shell/impl/cli/commands/agent/runtime/start.py#L21) |
| `agent stop` | — | I | `down` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_stop](../../../src/hyprial/shell/impl/cli/commands/agent/runtime/down.py#L15) |
| `agent unblock` | — | I | `agent.unblock` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_unblock](../../../src/hyprial/shell/impl/cli/commands/agent/admin.py#L71) |
| `agent restore` | — | I | `agent.restore-policy`, `agent.restore-threshold` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_restore](../../../src/hyprial/shell/impl/cli/commands/agent/restore.py#L14) |
| `agent grant` | — | I | `agent.grant`, `agent.grants`, `agent.revoke` | 已有 grant/grants/revoke 分支；本地授权规则移入现有 handler | [agent_grant](../../../src/hyprial/shell/impl/cli/commands/agent/grant.py#L14) |
| `agent grants` | — | I | `agent.grants` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_grants](../../../src/hyprial/shell/impl/cli/commands/agent/grant.py#L314) |
| `agent destroy` | — | I | `agent.destroy`, `agent.destroy.preview` | 保留方法名，execute 改收预览 operationId；--yes 也先预览 | [agent_destroy](../../../src/hyprial/shell/impl/cli/commands/agent/admin.py#L84) |
| `agent secret set` | — | N | — | 拟 agent.secret.set；stdin 输入留 CLI；校验/store/audit 归后端 | [agent_secret_set](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L471) |
| `agent secret rotate` | — | N | — | 拟 agent.secret.rotate；轮换、验证和重试策略归后端 | [agent_secret_rotate](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L500) |
| `agent secret remove` | — | N | — | 拟 agent.secret.remove；删除与撤销断言归后端，接入预览确认 | [agent_secret_remove](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L527) |
| `agent secret history` | — | N | — | 拟 agent.secret.history；历史查询和脱敏归后端 | [agent_secret_history](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L629) |
| `agent secret provider-write` | — | I | `agent.secret.provider-write` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_secret_entry_write](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L669) |
| `agent secret grant` | — | I | `agent.secret.grant` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_secret_grant](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L701) |
| `agent secret list` | — | N | — | 拟 agent.secret.entries.list；区分当前 grant 列表 IPC agent.secret.list 与 CLI 的凭据条目列表 | [agent_secret_list](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L734) |
| `agent secret revoke` | — | I | `agent.secret.revoke` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_secret_revoke](../../../src/hyprial/shell/impl/cli/commands/agent/secret.py#L778) |
| `agent home census` | — | I | `agent.home-census` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_home_census](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L18) |
| `agent home migrate preflight` | — | I | `agent.migrate.preflight`, `routine.list` | 本地 manifest/routine guard 移入现有 preflight | [agent_migrate_preflight](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L348) |
| `agent home migrate execute` | — | I | `agent.migrate.execute`, `routine.list` | 迁移 guard 与 manifest 校验移入现有 execute | [agent_migrate_execute](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L380) |
| `agent home migrate rollback` | — | I | `agent.migrate.rollback`, `routine.list` | rollback guard 移入现有 handler | [agent_migrate_rollback](../../../src/hyprial/shell/impl/cli/commands/agent/home.py#L407) |
| `agent config rebind` | — | I | `agent.config.rebind` | 复用现有 rebind；输入文件读取和用户确认与业务校验分开 | [agent_config_rebind](../../../src/hyprial/shell/impl/cli/commands/agent/config_rebind/__init__.py#L15) |
| `agent tiers` | G | N | — | 拟 agent.tiers；harness 能力表作为后端结果返回 | [agent_tiers](../../../src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py#L51) |
| `agent tiers probe` | — | I+N | `ps` | 拟 agent.tiers.probe；能力探测归后端；不可用结论不能伪造为成功 | [tiers_probe](../../../src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py#L75) |
| `agent provider login` | — | I | `agent.provider.default`, `agent.provider.login` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_provider_login](../../../src/hyprial/shell/impl/cli/commands/agent/provider/commands.py#L172) |
| `agent provider default` | — | I | `agent.provider.default` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_provider_default](../../../src/hyprial/shell/impl/cli/commands/agent/provider/commands.py#L299) |
| `agent provider logout` | — | I | `agent.provider.logout` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [agent_provider_logout](../../../src/hyprial/shell/impl/cli/commands/agent/provider/commands.py#L324) |
| `agent provider grant-key` | — | I | `agent.secret.grant`, `agent.secret.list` | 业务编排归后端；密钥输入不出现在 argv/日志 | [agent_provider_grant_key](../../../src/hyprial/shell/impl/cli/commands/agent/provider/grant_key.py#L42) |

## config

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `config set` | — | N | — | 拟 config.set；设置校验、持久化、生效时机归后端；离线不可用明确记录 | [config_set](../../../src/hyprial/shell/impl/cli/commands/config/settings.py#L14) |

## daemon

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `daemon status` | — | I | `ping`, `ps` | ps/ping 及恢复态行为保持；不新增本地状态权威 | [daemon_status](../../../src/hyprial/shell/impl/cli/commands/daemon/commands.py#L21) |
| `daemon start` | — | B | `ping`, `ps` | B03；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_start](../../../src/hyprial/shell/impl/cli/commands/daemon/commands.py#L76) |
| `daemon run` | — | B | — | B03；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_run](../../../src/hyprial/shell/impl/cli/commands/daemon/commands.py#L97) |
| `daemon stop` | — | B | `shutdown` | B03；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_stop](../../../src/hyprial/shell/impl/cli/commands/daemon/commands.py#L150) |
| `daemon restart` | — | B | `ping`, `ps`, `shutdown` | B03；在线子步骤仍复用相应 IPC，见设计 §5 | [restart_command](../../../src/hyprial/shell/impl/cli/commands/daemon/restart.py#L174) |
| `daemon service status` | — | B | `ping`, `ps`, `shutdown` | B04；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_service_status](../../../src/hyprial/shell/impl/cli/commands/daemon/service.py#L161) |
| `daemon service install` | — | B | `ping`, `ps`, `shutdown` | B04；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_service_install](../../../src/hyprial/shell/impl/cli/commands/daemon/service.py#L181) |
| `daemon service uninstall` | — | B | `ping`, `ps`, `shutdown` | B04；在线子步骤仍复用相应 IPC，见设计 §5 | [daemon_service_uninstall](../../../src/hyprial/shell/impl/cli/commands/daemon/service.py#L200) |
| `agent top` | — | I+N | `top.snapshot` | 拟 top.snapshot；扩展已有结果承接本地进程采样；CLI 只渲染 | [top_status](../../../src/hyprial/shell/impl/cli/commands/daemon/top.py#L439) |

## doctor

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `doctor` | — | B | `org.network`, `ping`, `ps`, `routine.audit`, `shutdown` | B06；在线子步骤仍复用相应 IPC，见设计 §5 | [doctor](../../../src/hyprial/shell/impl/cli/commands/doctor/commands.py#L977) |

## fs

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `fs ls` | — | I | `orgfs.ls` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_ls](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L64) |
| `fs resolve` | — | I | `orgfs.resolve` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_resolve](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L75) |
| `fs read` | — | I | `orgfs.read` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_read](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L103) |
| `fs write` | — | I | `orgfs.write` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_write](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L119) |
| `fs mkdir` | — | I | `orgfs.mkdir` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_mkdir](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L138) |
| `fs mv` | — | I | `orgfs.move` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_mv](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L148) |
| `fs rm` | — | I | `orgfs.remove` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_rm](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L163) |
| `fs history` | — | I | `orgfs.history` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_history](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L173) |
| `fs watch` | — | I | `orgfs.watch` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_watch](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L188) |
| `fs import` | — | I | `orgfs.import` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_import](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L213) |
| `fs export` | — | I | `orgfs.export` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_export](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L233) |
| `fs space list` | — | I | `orgfs.spaces` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_spaces](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L47) |
| `fs space create` | — | I | `orgfs.create` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_create](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L54) |
| `fs space join` | — | I | `orgfs.join` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_join](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L338) |
| `fs space web` | — | N | — | 拟 orgfs.web；授权 URL/启动所需事实归后端，浏览器打开留 CLI | [fs_web](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L347) |
| `fs space status` | — | I | `orgfs.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_status](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L452) |
| `fs space serve` | — | I | `orgfs.serve` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_serve](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L461) |
| `fs space checkout` | — | I | `orgfs.checkout` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_checkout](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L475) |
| `fs member invite` | — | I | `message.send`, `orgfs.invite`, `orgfs.spaces` | 邀请加消息通知的业务编排归后端；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_invite](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L257) |
| `fs member remove` | — | I | `orgfs.remove_member` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_remove_member](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L319) |
| `fs member list` | — | I | `orgfs.members` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_members](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L329) |
| `fs trash list` | — | I | `orgfs.trash` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_trash](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L426) |
| `fs trash restore` | — | I | `orgfs.restore` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_restore](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L436) |
| `fs purge plan` | — | I | `orgfs.purge_plan`, `targets` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_purge_plan](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L493) |
| `fs purge run` | — | I | `orgfs.purge`, `orgfs.space_purge` | 现有 purge/space_purge 计划与确认按 operationId 约束复核；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_purge](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L522) |
| `fs purge status` | — | I | `orgfs.purge_status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_purge_status](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L581) |
| `fs purge unban` | — | I | `orgfs.unban` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染；人类 URL 所需配置由 request 返回，CLI 不读取 home 业务配置 | [fs_unban](../../../src/hyprial/shell/impl/cli/commands/fs/commands.py#L595) |

## gui

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `gui` | — | D | — | 本批范围外；不作为 CLI 拆分前置条件 | [gui_command](../../../src/hyprial/shell/impl/cli/commands/gui/commands.py#L15) |

## host

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `host list` | — | I | `hosts` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [host_list](../../../src/hyprial/shell/impl/cli/commands/host/hosts.py#L42) |
| `host status` | — | P | — | 保留 COMING_SOON，无假成功 | [host_status](../../../src/hyprial/shell/impl/cli/commands/host/hosts.py#L89) |
| `host create` | — | P | — | 保留 COMING_SOON，无假成功 | [host_create](../../../src/hyprial/shell/impl/cli/commands/host/hosts.py#L98) |

## init

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `init` | — | B | `org.fetch`, `org.join`, `org.list`, `ping`, `ps`, `shutdown` | B01；在线子步骤仍复用相应 IPC，见设计 §5 | [init](../../../src/hyprial/shell/impl/cli/commands/init/init.py#L194) |
| `onboarding plan` | — | N | — | 拟 onboarding.plan；当前 home/config 快照及计划计算归后端 | [onboarding_plan](../../../src/hyprial/shell/impl/cli/commands/init/onboarding.py#L45) |
| `onboarding apply` | — | I+N | `adapter.pin`, `adapter.reload`, `agent.create`, `identity.bindings.list`, `identity.override.set`, `identity.users.list`, `management.squire.ensure` | 拟 onboarding.apply；agent/adapter/squire 配置编排归后端 | [onboarding_apply](../../../src/hyprial/shell/impl/cli/commands/init/onboarding.py#L162) |

## link

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `link list` | — | I | `link.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [link_list](../../../src/hyprial/shell/impl/cli/commands/link/commands.py#L40) |
| `link stats` | — | I | `link.stats` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [link_stats](../../../src/hyprial/shell/impl/cli/commands/link/commands.py#L65) |
| `link add` | — | I | `link.add` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [link_add](../../../src/hyprial/shell/impl/cli/commands/link/commands.py#L90) |
| `link drop` | — | I | `link.drop` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [link_drop](../../../src/hyprial/shell/impl/cli/commands/link/commands.py#L124) |

## log

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `log` | — | N | — | 拟 log.query；日志读取/筛选归后端；默认不列离线白名单 | [log_command](../../../src/hyprial/shell/impl/cli/commands/log/log.py#L87) |

## login

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `login` | — | B | `org.fetch`, `org.join`, `org.list`, `ping`, `ps`, `shutdown` | B02；在线子步骤仍复用相应 IPC，见设计 §5 | [login](../../../src/hyprial/shell/impl/cli/commands/login/commands.py#L21) |
| `logout` | — | B | `ping`, `ps`, `shutdown` | B02；在线子步骤仍复用相应 IPC，见设计 §5 | [logout](../../../src/hyprial/shell/impl/cli/commands/login/commands.py#L241) |

## message

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `message send` | — | I | `message.send` | 复用 message.send；附件处理/身份与路径授权归后端，CLI 可读取指定输入 | [send](../../../src/hyprial/shell/impl/cli/commands/message/send.py#L212) |
| `message ack` | — | I | `message.ack` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [ack](../../../src/hyprial/shell/impl/cli/commands/message/ack.py#L13) |
| `message reply` | — | I | `message.reply` | 复用 message.reply；附件/身份处理归后端 | [reply](../../../src/hyprial/shell/impl/cli/commands/message/reply.py#L16) |
| `message read` | — | I | `message.read_body` | 复用 message.read_body；必须保留 fetched 副作用和 pending 状态 | [read_body](../../../src/hyprial/shell/impl/cli/commands/message/read.py#L33) |
| `message batch` | — | I | `message.pending.batch` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [message_batch](../../../src/hyprial/shell/impl/cli/commands/message/observe/batch.py#L34) |
| `message inbox` | — | I | `message.query` | 复用 message.query；只读观察不得记录 fetched | [message_inbox](../../../src/hyprial/shell/impl/cli/commands/message/observe/inbox.py#L14) |
| `message status` | — | I | `message.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [message_status](../../../src/hyprial/shell/impl/cli/commands/message/observe/status.py#L14) |
| `message targets` | — | I | `targets` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [message_targets](../../../src/hyprial/shell/impl/cli/commands/message/observe/targets.py#L59) |
| `message trajectory` | — | I+N | `message.status` | 拟 message.trajectory；日志与消息状态组合归后端 | [message_trajectory](../../../src/hyprial/shell/impl/cli/commands/message/observe/trajectory.py#L364) |
| `message outbox list` | — | I | `message.query`, `outbox.list` | 查询组合及过滤规则归后端，CLI 保留展示选择 | [outbox_list](../../../src/hyprial/shell/impl/cli/commands/message/outbox.py#L95) |
| `message outbox prune` | — | I | `outbox.prune` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [outbox_prune](../../../src/hyprial/shell/impl/cli/commands/message/outbox.py#L122) |
| `message delivery status` | — | I | `message.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [delivery_status](../../../src/hyprial/shell/impl/cli/commands/message/delivery.py#L15) |

## meta

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `help` | — | P | — | 注册树生成帮助，无业务读取 | [help_command](../../../src/hyprial/shell/impl/cli/commands/meta/commands.py#L54) |
| `version` | — | B | — | B08；在线子步骤仍复用相应 IPC，见设计 §5 | [version](../../../src/hyprial/shell/impl/cli/commands/meta/commands.py#L67) |

## org

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `org create` | — | I | `org.create` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_create](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L238) |
| `org repair` | — | I | `org.repair` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_repair](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L263) |
| `org invite` | — | I | `org.invite` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_invite](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L291) |
| `org join` | — | I | `org.join` | 邀请/接入业务校验归现有 handler，非 bootstrap 绕行 | [org_join](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L505) |
| `org leave` | — | I | `org.leave` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_leave](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L521) |
| `org remove` | — | I | `org.remove` | 新增 org.remove.preview，execute 只接受预览 operationId | [org_remove](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L539) |
| `org delete` | — | I | `org.delete` | 新增 org.delete.preview，execute 只接受预览 operationId | [org_delete](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L573) |
| `org list` | — | I | `org.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_list](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L610) |
| `org pending` | — | I+N | `org.join`, `org.list` | 拟 org.pending；将当前 list/join 的业务选择与组合归后端 | [org_pending](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L627) |
| `org network` | — | I | `org.network` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_network](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L693) |
| `org acl grant` | — | I | `org.acl.grant` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [acl_grant](../../../src/hyprial/shell/impl/cli/commands/org/acl.py#L81) |
| `org acl revoke` | — | I | `org.acl.revoke` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [acl_revoke](../../../src/hyprial/shell/impl/cli/commands/org/acl.py#L113) |
| `org acl ls` | — | I | `org.acl.ls` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [acl_ls](../../../src/hyprial/shell/impl/cli/commands/org/acl.py#L136) |
| `org context validate` | — | N | — | 拟 org.context.validate；业务文档校验移到后端 | [org_context_validate](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L79) |
| `org context show` | — | N | — | 拟 org.context.show；有效 org 文档读取归后端 | [org_show](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L746) |
| `org context status` | — | N | — | 拟 org.context.status；本地同步/版本状态读取归后端 | [org_status](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L835) |
| `org context fetch` | — | I | `org.fetch` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [org_fetch](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L854) |
| `org context import` | — | I+N | `org.publish` | 拟 org.context.import.preview; org.context.import；文档差异/采用由后端执行，确认绑定预览 | [org_import](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L889) |
| `org group invite` | — | I | `org.invite` | 当前存在的独立注册路径；不因拆分改命令树，复用 org.invite | [org_invite](../../../src/hyprial/shell/impl/cli/commands/org/commands.py#L291) |

## profile

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `profile list` | — | B | — | B07；在线子步骤仍复用相应 IPC，见设计 §5 | [profile_list](../../../src/hyprial/shell/impl/cli/commands/profile/commands.py#L74) |
| `profile show` | — | B | — | B07；在线子步骤仍复用相应 IPC，见设计 §5 | [profile_show](../../../src/hyprial/shell/impl/cli/commands/profile/commands.py#L101) |
| `profile use` | — | B | — | B07；在线子步骤仍复用相应 IPC，见设计 §5 | [profile_use](../../../src/hyprial/shell/impl/cli/commands/profile/commands.py#L125) |
| `profile create` | — | B | — | B07；在线子步骤仍复用相应 IPC，见设计 §5 | [profile_create](../../../src/hyprial/shell/impl/cli/commands/profile/commands.py#L168) |

## routine

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `routine plan` | — | N | — | 拟 routine.plan；YAML 业务校验/展开归后端；离线行为有意改变 | [plan](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L105) |
| `routine add` | — | I | `routine.add` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_add](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L151) |
| `routine list` | — | I | `routine.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_list](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L216) |
| `routine status` | — | I | `routine.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_status](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L245) |
| `routine remove` | — | I | `routine.remove` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_remove](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L263) |
| `routine set` | — | I | `routine.set` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_set](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L283) |
| `routine pause` | — | I | `routine.pause` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_pause](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L312) |
| `routine resume` | — | I | `routine.resume` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [routine_resume](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L332) |
| `routine templates` | G | N | — | 拟 routine.templates.list；业务模板清单由后端返回 | [templates](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L32) |
| `routine templates show` | — | N | — | 拟 routine.templates.show；业务模板内容由后端返回 | [template_show](../../../src/hyprial/shell/impl/cli/commands/routine/commands.py#L52) |

## service

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `service` | G | P | — | 裸命令仅显示帮助 | [service](../../../src/hyprial/shell/impl/cli/commands/service/commands.py#L32) |
| `service connect` | — | I | `service.connect` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [connect](../../../src/hyprial/shell/impl/cli/commands/service/commands.py#L45) |
| `service disconnect` | — | I | `service.disconnect` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [disconnect](../../../src/hyprial/shell/impl/cli/commands/service/commands.py#L60) |
| `service list` | — | I | `service.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [list_services](../../../src/hyprial/shell/impl/cli/commands/service/commands.py#L75) |
| `service guide` | — | I | `service.guide` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [guide](../../../src/hyprial/shell/impl/cli/commands/service/commands.py#L89) |
| `service expose` | — | I | `network.expose` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [service_expose](../../../src/hyprial/shell/impl/cli/commands/service/network.py#L23) |
| `service unexpose` | — | I | `network.unexpose` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [service_unexpose](../../../src/hyprial/shell/impl/cli/commands/service/network.py#L54) |
| `service exposures` | — | I | `network.exposures` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [service_exposures](../../../src/hyprial/shell/impl/cli/commands/service/network.py#L72) |
| `service whois` | — | I | `network.peerKey` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [service_whois](../../../src/hyprial/shell/impl/cli/commands/service/network.py#L104) |

## socialware

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `socialware install` | — | P | — | 保留 COMING_SOON；遗留安装 helper 另归受控实现 | [install](../../../src/hyprial/shell/impl/cli/commands/socialware/commands.py#L19) |

## squire

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `squire setup` | — | I+N | `identity.bindings.list`, `identity.override.set`, `identity.users.list`, `management.squire.ensure` | 拟 squire.setup；组装已有 management/identity 操作，CLI 不读配置 | [squire_setup](../../../src/hyprial/shell/impl/cli/commands/squire/squire.py#L369) |
| `user-proxy status` | — | I+N | `ps` | 拟 user-proxy.status；合并状态与路由配置读取 | [user_proxy_status](../../../src/hyprial/shell/impl/cli/commands/squire/user_proxy.py#L41) |
| `user-proxy setup` | — | I+N | `adapter.pin`, `adapter.pins`, `agent.create`, `lifecycle.start`, `ps` | 拟 user-proxy.setup；注册/启动编排归后端 | [user_proxy_setup](../../../src/hyprial/shell/impl/cli/commands/squire/user_proxy.py#L70) |

## transfer

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `agent move` | — | N | — | 拟 transfer.move；当前迁移编排归后端；复用既有 transfer 阶段 | [register_session_commands.transfer](../../../src/hyprial/shell/impl/cli/commands/transfer/move.py#L15) |
| `agent transfer bundle-export` | H  | N | — | 拟 transfer.bundle.export；导出业务和状态读取归后端 | [register_bundle_commands.transfer_bundle_export](../../../src/hyprial/shell/impl/cli/commands/transfer/bundle.py#L16) |
| `agent transfer bundle-validate` | H  | N | — | 拟 transfer.bundle.validate；包格式与业务校验归后端 | [register_bundle_commands.transfer_bundle_validate](../../../src/hyprial/shell/impl/cli/commands/transfer/bundle.py#L158) |
| `agent transfer bundle-receive` | H  | N | — | 拟 transfer.bundle.receive；接收和落盘状态归后端 | [register_bundle_commands.transfer_bundle_receive](../../../src/hyprial/shell/impl/cli/commands/transfer/bundle.py#L220) |
| `agent transfer bundle-land` | H  | I | `transfer.land` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [register_bundle_commands.transfer_bundle_land](../../../src/hyprial/shell/impl/cli/commands/transfer/bundle.py#L279) |
| `agent transfer credential-seal` | H  | N | — | 拟 transfer.credential.seal；凭据封装归后端，不把值写日志 | [register_credential_commands.transfer_credential_seal](../../../src/hyprial/shell/impl/cli/commands/transfer/credentials.py#L15) |
| `agent transfer credential-open` | H  | N | — | 拟 transfer.credential.open；凭据解封和授权归后端 | [register_credential_commands.transfer_credential_open](../../../src/hyprial/shell/impl/cli/commands/transfer/credentials.py#L130) |
| `agent transfer bundle-complete` | H  | I+N | `agent.destroy` | 拟 transfer.bundle.complete；源端清理与销毁编排归后端 | [register_bundle_complete_command.transfer_bundle_complete](../../../src/hyprial/shell/impl/cli/commands/transfer/bundle.py#L331) |
| `agent transfer trust-source-add` | H  | N | — | 拟 transfer.trust-source.add；信任记录写入归后端 | [register_trust_commands.transfer_trust_source_add](../../../src/hyprial/shell/impl/cli/commands/transfer/trust.py#L61) |
| `agent transfer trust-source-list` | H  | N | — | 拟 transfer.trust-source.list；信任记录查询归后端 | [register_trust_commands.transfer_trust_source_list](../../../src/hyprial/shell/impl/cli/commands/transfer/trust.py#L153) |
| `agent transfer trust-source-retire` | H  | N | — | 拟 transfer.trust-source.retire；信任退役归后端 | [register_trust_commands.transfer_trust_source_retire](../../../src/hyprial/shell/impl/cli/commands/transfer/trust.py#L190) |
| `agent transfer precheck` | H  | I | `transfer.precheck` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [register_hidden_session_commands.transfer_precheck](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L18) |
| `agent transfer receive` | H  | I | `transfer.receive` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [register_hidden_session_commands.transfer_receive](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L40) |
| `agent transfer recipient-key` | H  | I | `transfer.recipientKey` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [register_hidden_session_commands.transfer_recipient_key](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L74) |
| `agent transfer cred-stage` | H  | N | — | 拟 transfer.credential.stage；临时凭据接收/staging 归后端 | [register_hidden_session_commands.transfer_cred_stage](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L95) |
| `agent transfer container-home` | H  | N | — | 拟 transfer.container-home；容器 home 定位/业务规则归后端 | [register_hidden_session_commands.transfer_container_home](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L149) |
| `agent transfer session-path` | H  | N | — | 拟 transfer.session-path；会话路径规则归后端 | [register_hidden_session_commands.transfer_session_path](../../../src/hyprial/shell/impl/cli/commands/transfer/internal.py#L174) |
| `agent transfer runtime downgrade-state` | H  | B | — | B09；在线子步骤仍复用相应 IPC，见设计 §5 | [register_runtime_commands.transfer_runtime_downgrade_state](../../../src/hyprial/shell/impl/cli/commands/transfer/runtime.py#L12) |
| `agent transfer runtime probe` | H  | N | — | 拟 transfer.runtime.probe；runtime 检查归后端，独立于停机 downgrade 白名单 | [register_runtime_commands.transfer_runtime_probe](../../../src/hyprial/shell/impl/cli/commands/transfer/runtime.py#L26) |

## upgrade

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `daemon upgrade` | G | B | `ping`, `ps`, `shutdown` | B05；在线子步骤仍复用相应 IPC，见设计 §5 | [upgrade](../../../src/hyprial/shell/impl/cli/commands/upgrade/commands.py#L696) |
| `daemon upgrade status` | — | I | `autoupdate.status`, `ping`, `ps` | 复用 autoupdate.status；移除本地业务 fallback，离线变化需明确记录 | [autoupdate_status](../../../src/hyprial/shell/impl/cli/commands/upgrade/autoupdate.py#L25) |
| `daemon upgrade apply` | — | B | `ping`, `ps`, `shutdown` | B05；在线子步骤仍复用相应 IPC，见设计 §5 | [autoupdate_restart](../../../src/hyprial/shell/impl/cli/commands/upgrade/autoupdate.py#L221) |
| `daemon upgrade run` | H  | B | `autoupdate.trigger`, `ping`, `ps`, `shutdown` | B05；在线子步骤仍复用相应 IPC，见设计 §5 | [autoupdate_run](../../../src/hyprial/shell/impl/cli/commands/upgrade/autoupdate.py#L308) |

## user

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `user list` | — | I | `identity.bindings.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_list](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L282) |
| `user bind` | — | I | `identity.override.set` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_bind](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L297) |
| `user unbind` | — | I | `identity.override.clear` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_unbind](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L332) |
| `user whois` | — | I | `identity.resolve` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_whois](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L359) |
| `user add` | — | I | `identity.users.add` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_add](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L388) |
| `user show` | — | I | `identity.users.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_show](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L451) |
| `user bindings audit` | — | I | `identity.bindings.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_bindings_audit](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L600) |
| `user bindings withdraw` | — | I | `identity.binding.withdraw` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_bindings_withdraw](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L638) |
| `user bindings publish` | — | I | `identity.binding.publish` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [user_bindings_publish](../../../src/hyprial/shell/impl/cli/commands/user/commands.py#L652) |

## work

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `org work add` | — | I | `org.list`, `work.add` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_add](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L105) |
| `org work check` | — | I | `org.list`, `work.check` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_check](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L133) |
| `org work note` | — | I | `org.list`, `work.note` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_note](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L149) |
| `org work status` | — | I | `org.list`, `work.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_status](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L165) |
| `org work done` | — | I | `org.list`, `work.done` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_done](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L181) |
| `org work repair` | — | I | `org.list`, `work.repair` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_repair](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L196) |
| `org work link` | — | I | `org.list`, `work.link` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_link](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L211) |
| `org work keywords` | — | I | `org.list`, `work.keywords` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_keywords](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L227) |
| `org work show` | — | I | `org.list`, `work.show` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_show](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L249) |
| `org work resolve` | — | I | `org.list`, `work.resolve` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_resolve](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L275) |
| `org work ls` | — | I | `org.list`, `work.ls` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [work_list](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L291) |
| `org work collect` | — | I+N | `org.list` | 拟 work.collect；关联任务轮次计算归后端 | [work_collect](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L315) |
| `org work finish` | — | I+N | `org.list` | 拟 work.finish；输入文件读取留 CLI；关联轮次结算归后端 | [work_finish](../../../src/hyprial/shell/impl/cli/commands/work/commands.py#L365) |

## workflow

| 命令（省略 hyprial） | 标记 | 类别 | 已有 IPC 线索 | 目标 / 验收边界 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `workflow plan` | — | I | `workflow.expansion.plan`, `workflow.plan` | 将 YAML/expansion 的业务校验收入现有方法 | [plan](../../../src/hyprial/shell/impl/cli/commands/workflow/run.py#L101) |
| `workflow run` | — | I | `workflow.plan`, `workflow.start` | plan/start 的业务编排归后端；CLI 不绕回 PAC 实现 | [run](../../../src/hyprial/shell/impl/cli/commands/workflow/run.py#L164) |
| `workflow list` | — | I | `workflow.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [list_workflows](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L25) |
| `workflow inspect` | — | I+N | `workflow.node.inspect`, `workflow.status` | 拟 workflow.node.context；普通 inspect 复用现有 IPC；--context 不再直接读 SQLite | [inspect](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L64) |
| `workflow cancel` | — | I | `workflow.cancel` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [cancel](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L117) |
| `workflow events` | — | N | — | 拟 workflow.events；snapshot/cursor-poll；不 tail 本地业务数据库 | [events](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L125) |
| `workflow reset` | — | I | `pac.flag.reset` | 复用 pac.flag.reset；不能把 reset 当成任意失败重试 | [reset](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L65) |
| `workflow complete` | — | I | `workflow.complete` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [complete](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L176) |
| `workflow fail` | — | I | `workflow.fail` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [fail](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L224) |
| `workflow gc` | — | I | `pac.gc.preview` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [workflow_gc](../../../src/hyprial/shell/impl/cli/commands/workflow/misc.py#L22) |
| `workflow history list` | H  | I | `workflow.history.list` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [history_list](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L177) |
| `workflow history status` | H  | I | `workflow.history.status` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [history_status](../../../src/hyprial/shell/impl/cli/commands/workflow/inspect.py#L185) |
| `workflow overview show` | — | I+N | `org.list`, `ps` | 拟 workflow.overview；模型聚合/授权过滤归后端 | [overview_show](../../../src/hyprial/shell/impl/cli/commands/workflow/overview.py#L79) |
| `workflow overview publish` | — | I+N | `org.list`, `ps` | 拟 workflow.overview.publish；发布与发布记录归后端，CLI 不维护 publish.json | [overview_publish](../../../src/hyprial/shell/impl/cli/commands/workflow/overview.py#L108) |
| `workflow worker stop` | — | I | `workflow.worker.stop` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [worker_stop](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L103) |
| `workflow worker restart` | — | I | `workflow.worker.restart` | 复用现有方法；领域 helper/身份或业务校验下沉，CLI 仅传参和渲染 | [worker_restart](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L112) |
| `workflow notify resend` | — | N | — | 拟 workflow.notify.resend；重发选择和执行归后端 | [notify_resend](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L121) |
| `workflow migration status` | H  | N | — | 拟 workflow.migration.status；迁移报告读取归后端 | [migration_status](../../../src/hyprial/shell/impl/cli/commands/workflow/control.py#L140) |

## 复核方法与限制

1. checkout 上述基线；设置新建临时 HYPRIAL_HOME / HARNESS_STATE_DIR / HARNESS_SOCKET_PATH，
   并设置 HYPRIAL_NETWORK_ISOLATED=1，之后才导入 CLI。
2. 用 `typer.main.get_command(entry.app)` 递归 `list_commands/get_command`；保留隐藏父组标记。
   叶子全部列入；有 callback 且 invoke_without_command 的组另列一行。
3. `inspect.unwrap(command.callback)` 得到源码 callback，记录模块、限定名和源码位置。
   本次所有 callback 均来自目标工作树，不使用安装包中的 CLI 源码替代。
4. 结合 AST 的方法常量/字面量与 helper 调用读源码，人工确定 I/N/B/P/D 与目标方法。
   注册树证明入口覆盖，AST 是调用线索，分类/新方法是设计提案；三者不能混成运行验收。
5. 更新基线时重新枚举，比较完整命令路径集合；不能仅保持总数 227 就判定清单仍正确。

拟删除的旧 Python import/兼容绑定不在用户命令树中。DR-30/31 必须另查生成 argv、
hooks、安装入口、跨命令 helper、tests/scripts/contract 的消费者；不能只修本表中的回调。
