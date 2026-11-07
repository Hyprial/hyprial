# Tailnet 替换 M0 准备：依赖台账、落点与待裁决项

日期：2026-10-03。上游计划：[tailcat-casdoor-experiment.md](tailcat-casdoor-experiment.md) §13–§18（PR #1106）。
基线：`refactor/reconstruct-modules` @ `f87f3adf`（PR #1106 已合入）**加未提交的深模块重构工作树**。

## 0. 本文范围与状态

本文只完成 §14.2 **M0「固定迁移契约与对象清单」中能从源码得出的部分**：在重构后的树上重核依赖、给出新代码落点和工作包切分，并列出必须由人裁决才能冻结的项。它不启动 M1–M7 的实现、派单、发布或任何生产操作；§14.1 的授权边界不变。

| M0 产物（§14.2） | 状态 |
|---|---|
| 重构后候选基线 SHA | **BLOCKED**：重构仍在未提交的工作树中（约 1600 个改动条目）。重构提交落地后，用那个 SHA 重跑 §1 的扫描命令 |
| 功能 → 旧依赖 → 新位置对照 | **SOURCE_VERIFIED**（本文 §1–§2；路径对照另见上游 §13.6 末表） |
| 平台/版本与节点清单、组织/账号/设备/URI 对照 | **UNKNOWN**：需要生产读数，本文不读取生产状态 |
| 离线节点处置、验收阈值、迁移组织与窗口 | **待裁决**（§4） |

## 1. 重构树上的依赖台账

扫描模式：`tsnet|headscale|head\.hyprial|preauth|run_join|tailscale status|tailscale whoami|LocalAPI|WhoIs|MagicDNS|\.ts\.net|tailscaled|tailnet`（不区分大小写）。grep 命中只用来定位，不能证明运行时依赖（§14.5），每一项仍需按调用关系确认。

| 表面 | 命中文件数 | 说明 |
|---|---|---|
| `src/hyprial/**/*.py` | 47 | 见下表按 §13.2 类别归并 |
| `sidecar/hyprial-tsnet/` | 37 | Go 边车，4458 行；协议 v2 ops：`up/status/down/map-peer/unmap-peer/expose/unexpose/whois`，事件 `hello/state/peer-mapped` |
| `tests/` | 46 | 含 `test_tsnet_*`、`test_forwarding_*`、`test_login*`、`test_e2e_headscale_prod.py`、`test_orgfs_webserver.py`、`test_desktop_network.py` 等 |
| `contract/` | 9 | `e2e/22-public-install-tailnet`、`run-public-install-tailnet.py`、`scenarios.json`、uv 安装流、`migration/drill-guard.py` |
| `.forgejo/` | 6 | `workflows/sidecar.yml`、`e2e-controlled.yml`、`github-publish*`、`ci-downloads.json` |
| `scripts/` | 13 | 未逐个归类，M6 处理 |
| `src/gui/src`、`desktop/src` | 0 | GUI 经 Python `shell/impl/desktop_network.py` 间接依赖 |

Python 侧按 §13.2 的八类归并（路径相对 `src/hyprial/`）：

| 类别 | 主要文件（命中数） | 去向（工作包） |
|---|---|---|
| S1 登录/入网/profile | `daemon/impl/network/tsnet_join.py`(58)、`daemon/impl/configuration/network_profile.py`(15，`head.hyprial.ai` 默认值、`CONTROL_PLANE_KINDS=("tailscale","headscale")`)、`shell/impl/cli_commands/setup/{login,login_flow,first_run}.py`、`shell/impl/cli_commands/config/{network,profile}.py` | M3：身份→组织→设备登记→ready；profile 版本化迁移 |
| S2 Desktop ready | `shell/impl/desktop_network.py`（读 `state/tsnet/node.json`，调用 `run_join`） | M3：与 CLI 共用同一 ready 判定 |
| S3 发现/转发/重连 | `daemon/impl/bootstrap/discovery.py`(23，`ForwardingEndpoints`、宿主 `tailscale status`)、`daemon/impl/application/netendpoints/{endpoints,forwarding}.py`(24/1)、`daemon/impl/forwarding/{controller,supervisor,policy}.py`、`daemon/impl/forwarding_config.py`、`daemon/impl/network/peer_reachability.py` | M2：认证目录 → Tailcat 适配器 → loopback 入口 → Zenoh connect（§17.1）；不打开 gossip |
| S4 业务信任边界 | `daemon/impl/dispatch/remote/__init__.py`（「trusted tailnet message plane」）、`daemon/impl/orgfs/network/ingress.py`、`daemon/impl/inbox/links/zenoh_link.py`、`daemon/impl/transport/zenoh/config.py` | M1+M2：把「受信任 tailnet」假设改成明确的组织/主体授权；Transport 结果语义不变 |
| S5 OrgFS 网页与 WhoIs | `daemon/impl/orgfs/web.py`(20，MagicDNS FQDN)、`daemon/impl/orgfs/webserver/{identity,config,server}.py`、`shell/impl/cli_commands/orgfs/fs.py`（`--whois-socket`）、`daemon/impl/ipc/router.py`（`network.whois`） | M3（边车承载的入口）/ M6（宿主 LocalAPI 入口）：可信身份改由 `PeerKey` → 设备 → 组织映射；§16.5 cookie 隔离 |
| S6 transfer | `daemon/impl/transfer/orchestrator.py`、`daemon/impl/transfer/execution/ssh.py` | M6：§16.6 临时 Tailcat 转发 + 任务级 `-F` SSH 配置 |
| S7 身份/URI | `identity/impl/tailnet_identity.py`(45，`tailscale whoami/status`)、`identity/interface.py`、`shell/impl/cli_commands/setup/selfhost.py`、`daemon/impl/configuration/identity.py`（`resolve_node_owner`）、`kernel/impl/primitives/uri.py` | M0 裁决 self-host 去留；URI/owner/nodeId 保持稳定（§14.3） |
| S8 安装/发布/诊断 | `daemon/impl/network/tsnet_sidecar.py`(31)、`daemon/impl/configuration/home_migration.py`、`daemon/impl/installers/release/vocabulary.py`、`shell/impl/cli_commands/observe/doctor.py`、`.forgejo/workflows/sidecar.yml` | M3（新资产）/ M6（删旧资产与专属 E2E） |

`daemon/interface.py` 中有 16 处命中：daemon 公共面导出了 7 个旧网络符号，shell 由此消费：`run_join`、`JoinOutcome`、`default_open_url`（`network/tsnet_join`），`install_sidecar`、`verify_installed_sidecar`、`SidecarError`（`network/tsnet_sidecar`），`TailscaledWhoisResolver`（`orgfs/webserver/identity`）。替换时这一层是契约面，需要保持跨域只走 `interface.py`（AGENTS.md 模块边界）。

## 2. 新代码落点（建议，待 M0 评审）

按 kernel → identity → daemon → biz → shell 分层，新组件只进各域 `impl/`，对外只经 `interface.py`：

| 新组件 | 建议位置 | 理由 |
|---|---|---|
| Tailcat 引擎进程 | 新 Go 模块 `sidecar/hyprial-tailcat/`（独立版本与 tag，同 tsnet 模式） | Tailcat 是 Go 库。沿用 stdio JSON 边车边界，Python 侧改动面最小；不复用 tsnet 的 `up`（Headscale join）语义 |
| 边车协议 v3 | `sidecar/hyprial-tailcat/internal/proto` + `contract/` 新契约 | 建议 ops：`hello/start/status/allow/disallow/disconnect/dial-map/unmap/expose/unexpose/peer-key`。`whois` 改为返回完整公钥，由 Python 映射主体。属于已发布语义变更，必须同步 `contract/` |
| 设备密钥与登记 PoP | `identity/impl/device/`（新） | 设备身份属于 identity 域；私钥只在边车/受控文件，不进 argv/日志（§18.2） |
| 组织目录与授权客户端 | `identity/impl/org_directory/`（新，消费 Casdoor + 控制面 API） | 「谁属于哪组、哪个 key 是哪台设备」是身份问题，daemon 只消费结果 |
| 连接适配与转发监督 | 新子包 `daemon/impl/network/tailcat/`，最终替换 `tsnet_join.py`/`tsnet_sidecar.py`；`forwarding/` 复用 supervisor 结构 | 保留 `ForwardingEndpoints` → Zenoh connect 的形状（§17.1） |
| 组级授权执行点 | `daemon/impl/dispatch/remote/`、`daemon/impl/orgfs/network/ingress.py`、`orgfs/webserver/identity.py` | 把「trusted tailnet」注释所描述的隐式信任改为显式检查 |
| CLI/Desktop 入口 | `shell/impl/cli_commands/setup/`、`shell/impl/desktop_network.py` | 只改编排，不复制权威逻辑 |
| 控制面服务（M1） | **待裁决**：放在本仓之外还是本仓 `biz/` | §14.2 把 M1 列为独立责任方；服务宿主与存储未定 |

硬约束提醒：每个代码目录最多 7 个直接文件、每个文件最多 1000 行。`daemon/impl/network/` 现已有 7 个直接文件（`__init__`、`peer_reachability`、`proxy_route`、`route_delivery`、`route_ports`、`tsnet_join`、`tsnet_sidecar`），所以新实现只能放进子包，不能平铺新增；删除 tsnet 文件属于 T2/M3 的一部分。

## 3. 建议的执行顺序（M0 → M2 骨架，均 NOT_RUN）

1. **M0-a 基线**：重构提交落地后取 SHA，重跑 §1 扫描并更新本表；把 `daemon/interface.py` 中的网络符号列成契约清单。
2. **M0-b 裁决**：完成 §4 全部项目，并把结果写回本文（失联韧性：决策不能只留在消息里）。
3. **M2-spike**（可以与 M1 并行，使用假控制面）：`sidecar/hyprial-tailcat` 最小骨架（固定 Tailcat `b4dc28e8…`）+ 协议 v3 契约测试 + 两 daemon 经 loopback 入口完成 Zenoh 往返。隔离 home/端口，强制本地 DERP，按三态报告结果。
4. **M1 契约评审**：设备 PoP、目录 API、权限版本与租期、撤权传播。在实现前冻结。
5. 后续 M3–M7 按上游 §14.2 推进。

## 4. 裁决记录与待裁决项

2026-10-03 Allen 两轮答复后的状态：

| # | 事项 | 状态 | 内容 / 依据 |
|---|---|---|---|
| D1 | DERP/STUN 来源 | **已裁决：使用 Tailscale 官方 DERP** | 约束：Tailcat 首次连接与地址交换都要经过 DERP（上游 §17.2），因此官方 DERP 是启动依赖，不只是兜底中继；共享 DERP 有速率限制（Tailcat CHANGELOG 关于 `perf --via-derp` 的说明），验收时需要分别记录直连和中继性能；不能指向 Headscale 内嵌 DERP |
| D2 | 控制面状态放在哪里 | **已裁决（第二轮）** | Casdoor（auth.hyprial.com）只承载标准 IAM 内容：用户、组织（Group）、成员、角色与权限。Hyprial 特有的数据——设备 Tailcat 公钥绑定、节点/服务目录、key 代次与租期——存放在 **OrgFS**；操作入口是 `hyprial org`。M1 需要解决的设计点：① 首次接入的引导问题（OrgFS 本身经网络同步，新设备在拿到目录之前无法连网，邀请需要携带种子节点的连接资料）；② OrgFS 写入授权（现有 ingress 依赖「受信任 tailnet」假设，必须改为由设备持钥签名并对照 Casdoor 成员资格校验）；③ 带 PSK 的连接地址不能以明文进入组织内可广泛读取的 OrgFS 文档 |
| D3 | Tailcat 版本 | **已裁决（第二轮）：固定在实验提交 `b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f`**，有正式发布后再议 | 该提交包含 `AllowClient` 钩子与 `DisconnectClient`；v0.7.0 不含（见第一轮记录） |
| D4 | 用户身份来源 | **已裁决（第二轮）：完全退役 self-host 以及所有不经 auth.hyprial.com 的用户身份创建路径** | 退役对象：`hyprial init` 的 self-host 分支、`identity/impl/tailnet_identity.py`、`setup/selfhost.py`；不新增本地模式。`HYPRIAL_OWNER` **保留**（第三轮裁决），只作测试/托管覆盖，不作为产品身份路径；已有 self-host home 需要显式迁移提示（owner 会变，旧 URI 失效） |
| D5 | §14.5 验收数值 | **已裁决（第二轮）：采纳默认值** | 撤权 ≤30s、离线租期 ≤5min、重连 ≤60s、已确认消息与文件零丢失零重复、切换窗口 30min（含 15min 回滚） |
| D6 | `org` 新子命令与网络状态命令 | **部分裁决（第三轮）**：增加 `hyprial org network`；新增 `hyprial org join <link>`（原 `org execute`；CLI 已于 R3 删除、并入 `join`，见 D2 流程） | 其余子命令名（create/invite/join/leave/remove/delete/list）在 M1 前确定 |
| D7 | 首批切换组织/窗口 | **移出 M0**，到 M4/M5 再定 | 只影响切换执行，不影响开发 |
| D8 | 工作包责任人 | **已裁决（第二轮）：M0–M7 均由 Claude 负责** | 执行节点用 pi-subworker（pi 调用 glm/kimi/deepseek，按档选择）；开发在独立 worktree `tailnet/cutover` 中进行，完成后交给重构主 agent 合并；Allen 评审 |

### 4.1 D2 组织网络建立流程（Allen 第三轮构想，待细化）

1. org-owner 创建 org network，同时建立该组织新的 OrgFS 空间；owner 的 daemon 是首个节点。
2. org-owner 用 `hyprial org invite` 邀请新用户。被邀请者执行通用命令 `hyprial org join <link>`：daemon 先确认链接来源属于已授权范围，再读取链接指向的 workflow yaml 并执行。执行过程中与 org-owner 建立连接，并获得 OrgFS 访问权。整个流程由 hyprial 处理。
3. 被邀请者通过 OrgFS 中的信息与其他成员建立连接，并读取初始化所需的其他信息。

这个流程解决了此前 D2 设计点 ①（新设备首次接入）：邀请链接本身就是引导入口。待细化：链接的签名与一次性/时效约束；join 入口在 Tailcat 准入层如何接纳尚未登记的设备 key；workflow yaml 允许的动作范围；成员记录在 OrgFS 中的写入授权；撤权后的 key/PSK 处理；owner 离线时由谁接纳邀请。

### 4.2 D2 细化：OrgFS 写入授权复用 Casbin（Allen 第四轮认可）

| 问题 | 方案 |
|---|---|
| 授权：某角色能对某 OrgFS 路径做什么 | 复用 Casbin 模型。策略的权威存放在 Casdoor（RBAC with domains，域 = 组织；路径用 keyMatch 匹配，例如 owner/admin 可写 `/members/*`，member 只能写 `/devices/<self>/*`） |
| 执行点 | 每个 daemon 在 OrgFS ingress 本地执行：用 pycasbin 加载与 Casdoor 相同的 model，定期拉取策略并缓存，带策略版本与租期（≤5min，D5），过期即拒绝写入；不对每条更新在线调用 `/api/enforce` |
| 认证：这条更新是谁写的 | Casbin 不负责。每条 CRDT 更新携带作者设备签名；设备记录由管理员签名，信任链根是创建组织时生成的 org 根公钥，存于 Casdoor Group 属性，作为唯一信任锚 |
| 成员名单权威 | Casdoor 为权威；OrgFS meta 中的成员表只作缓存 |
| 待验证 | Casdoor 自定义 model 能否表达「只能写自己的设备记录」（matcher 中比较 `r.sub` 与路径）；引入 pycasbin 需过依赖审查（主集成持有 `pyproject.toml`/`uv.lock`，走 integration-request） |

### 4.3 本轮实施目标（Allen 第四轮）

目标是 `refactor/reconstruct-modules` 合入 dev 时，Tailcat 替换已经体现在新的深模块结构中，同时清理旧代码和死代码（Headscale/Tailscale 依赖、tsnet 依赖、kanban/socialware 等旧设计）。**全功能跑通不是本轮目标。**

- 要求：结构完整，基本功能流程跑通（登录 → 设备 → 组织 → 两 daemon 经 Tailcat 通信）。
- 权限相关（§4.2 签名、Casbin 执行、撤权时延）本轮可以不严格实现：保留接口与接线点，实现可以宽松，但必须在代码和总账中标明。
- 测试可以使用占位测试：断言恒真，同时发出 `This is a fake test` 警告。占位测试在总账中单独计为 FAKE，不计入 PASS。
- 协作边界与交付格式遵循重构主 agent 的指令（2026-10-03）：在独立 worktree 中承接冻结快照并实施，不提交 overlay，不自动 commit/push/PR，最终交付证据目录。

## 5. 未执行与边界

- 未读取生产 daemon、Headscale、节点密钥或用户配置；§1 只是源码扫描。
- 未运行任何 Tailcat、DERP 或跨机实验；第 12/16/18 节的实验结论不能抵扣 §14.5 的任何验收项。
- 团队共享 Headscale 自始至终保留（§14.1）。
