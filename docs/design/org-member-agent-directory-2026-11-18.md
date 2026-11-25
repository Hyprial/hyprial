# 组织成员 Agent 目录（2026-11-18）

## 决策

组织 Agent 列表通过已确认的组织成员关系，汇总成员各自设备发布的 Agent 清单。列表可见性不授予 Agent 的组织级操作权限。GUI 通过现有 `targets` 读取可聊天的成员 Agent，按 `orgs` 分组，`orgDirectoryStatus` 表示每个组织的清单是否完整；本轮不新增 IPC 方法。

成员设备在组织 OrgFS 的 `directory/people/<成员键>/agents-<设备>.json` 发布最小清单：`agentId`、URI、名称、owner、设备 ID。文档受现有 people 路径作者规则保护；读取时再次校验文档 ID、作者、内容与当前成员集合。成员退出后，不论旧文档是否仍在本地投影，都不展示其 Agent。任何成员清单尚未同步或不可读时，结果标记为不完整，不把未知当成空。

发布由现有组织目录定期刷新驱动；Agent 注册表发生变更后，下一次刷新发布新快照。发布失败不阻止 Agent 创建或启动。读取只查询本地已同步 OrgFS 投影，成员离线时保留最后已确认的清单。多个设备的 Agent 按不可变 `agentId` 去重，远端条目只显示，不提供本机管理操作。

## 实施步骤

1. 为成员清单存储写失败测试，覆盖受保护路径、作者/内容校验、当前成员过滤、缺失清单的不完整状态。实现读写适配器。
2. 为只读 daemon MethodSpec 和发布刷新写失败测试。实现组织目录发布与读取入口，更新 OpenAPI 契约和示例。
3. 为 GUI 写失败测试，覆盖一名本机成员一个 Agent、另一成员三个 Agent、离线缓存、退出组织、读取不完整。接入新方法并保持远端管理禁用。
4. 运行结构与模块边界检查、焦点 Python/GUI 测试、Ruff；因修改 `src/hyprial/`，执行全量 `tests/`。用隔离双账号和真实 Tailcat/OrgFS/浏览器验收，不读取生产凭据或改动日常 daemon。

## 验收

两名成员以不同设备 ID 加入同一工作组织，各创建 1/3 个 Agent，不调用 `org.agent.join`。任一成员在 Web 切换到该组织后看到四个 Agent；另一成员设备离线后仍可见已同步清单；成员退出后其 Agent 不再出现在该组织列表。读取失败必须显示结果不完整。明确区分组件测试与真实网络验收结论。

## 2026-11-19 验证记录

- PASS：隔离的两个已登录 home 分别以测试设备 A 和测试设备 B 运行真实 Tailcat、OrgFS 与 daemon，同属测试工作组织。A 注册 1 个 Agent，B 注册 3 个 Agent；A 的 `agent.list` 只有 1 个，A 与 B 的 `org.member-agents.list` 都返回 `status=ok` 和同样的 4 个 Agent。两个账号之间的 sidecar 映射与 splice 已建立。
- PASS：真实 OrgFS 回归测试覆盖已存在的 `directory` 路径。OrgFS 在此情况下以 `invalid-argument` 返回“节点已存在”，发布器会再次确认该路径确为目录，然后继续发布。
- PASS：组织相关 Python 焦点测试、前端目录与生命周期焦点测试、Ruff、OpenAPI 快照与 Python 正则契约快照。
- PASS：组织相关 Python 测试共 521 项；结构限制、Python 模块边界、GUI TypeScript 类型检查及 CI scope/plan 测试通过。全量 GUI 首轮 2854 项中 10 项失败，已修正相关旧断言并重跑涉及的 11 个文件；这 11 个文件最终通过，全量 GUI 未再次运行。
- FAIL：全量 Python 以 `libcst==1.9.0` 收集并运行至 457 项时，未改动的 Lark adapter shutdown 用例报告 effect custody 超时；单独重跑该用例通过。全量 Python 尚无通过结论。
- UNKNOWN：真实浏览器点击组织切换、成员离线后的持久缓存，以及成员退出后的跨设备同步尚未实测；这些场景有组件测试覆盖。

## 2026-11-19 入口收敛

上面的双账号 PASS 是原 `org.member-agents.list` 入口的历史记录。目录数据仍由受保护的 OrgFS 成员文档提供；daemon 在现有 `targets` 响应中给 Agent 行附 `orgs`，给组织附 `orgDirectoryStatus`。GUI 只对 `targets` 返回的远端 Agent 提供聊天入口，本机管理操作仍不可用。

2026-11-20 使用两个独立 home/state、真实 daemon 和本机 Tailcat relay 验证：两个账号加入同一组织后，双方的 `targets(kind=agent)` 均返回对方普通 Agent；A 通过现有 `message send` 向 B 的普通 Agent 发送，B 的 inbox 收到同一 messageId。B 离开组织后，A 的组织设备目录不再包含 B，`targets` 也不再返回 B 的 Agent。此时 OrgFS 仍可能暂存旧成员清单，因此 `targets` 还必须按当前设备目录过滤。GUI 的点击入口由组件测试验证；真实浏览器被首次设置流程挡住，未完成端到端点击验证。系统 `hyprial-user` Agent 的地址会映射为用户消息路径，本次跨账号用户消息回执超时，不属于普通 Agent 投递的通过范围。
