# 既有 IPC 方法逐项清单（设计附录）

基线：`4a51775186e3e6087bde7c5826980d195333ed65`。主设计见 [design.md](design.md)。

2026-10-28：这里的既有方法名是盘点事实，不意味着全部名称在 move 改造后保留。
move 的目标契约仍待确认，变更范围见[跨线协调记录](coordination-2026-10-28.md)。
本表 DR 编号是首版范围索引，不是当前已建工作项；A 的早期声明/门禁与 C 的完整覆盖见 [A 提案](cli-surface.md)。

共 **195 个不同的方法名**。全部是当前 daemon 已有的分发面，目标均为保留唯一业务实现、
补全 MethodSpec 和 contract；方法内部重构不能另立 CLI 专用实现。

盘点方法：解析 router 的方法比较、IPC 常量、org network/ACL 分发表，继续检查
`agent.*`、`identity.*`、`orgfs.*`、`work.*` 等前缀分发的实际下游方法分支。
182 个名称来自比较分支，13 个来自 org 分发表，与 195 项 metrics 集合逐项相等；
该相等只是交叉检查，metrics 集合不是注册权威。

来源列是分发证据，不一定是完整 handler 的定义。任务列表示声明/契约的首要归属，
内部方法或未随命令族结项的方法还须由 DR-29 收口，不能因为没有 CLI 调用而删除。
CLI 线索列来自 [CLI 清单](cli-inventory.md) 的静态分析，`—` 不表示没有 MCP/worker/其他消费者。

**当前传输全部是一请求一 JSON 响应。** message.pending.wait 等可在一次调用内等待，
不代表已有多帧流协议。未来 cursor-poll 的每次调用仍是一个 JSON；mode、错误、授权、
重试和副作用按主设计 §6/8 逐方法声明。

## 方法清单

| IPC 方法 | 任务 | CLI 命令族线索 | 分发来源 |
| --- | --- | --- | --- |
| `adapter.list` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:649](../../../src/hyprial/daemon/impl/ipc/router.py#L649) |
| `adapter.pin` | DR-19 | `adapter`, `init`, `squire` | [daemon/impl/ipc/router.py:657](../../../src/hyprial/daemon/impl/ipc/router.py#L657) |
| `adapter.pins` | DR-19 | `squire` | [daemon/impl/ipc/router.py:661](../../../src/hyprial/daemon/impl/ipc/router.py#L661) |
| `adapter.reload` | DR-19 | `adapter`, `init` | [daemon/impl/ipc/router.py:647](../../../src/hyprial/daemon/impl/ipc/router.py#L647) |
| `adapter.start` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:653](../../../src/hyprial/daemon/impl/ipc/router.py#L653) |
| `adapter.status` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:651](../../../src/hyprial/daemon/impl/ipc/router.py#L651) |
| `adapter.stop` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:655](../../../src/hyprial/daemon/impl/ipc/router.py#L655) |
| `adapter.unpin` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:659](../../../src/hyprial/daemon/impl/ipc/router.py#L659) |
| `agent.config.rebind` | DR-18 | `agent` | [daemon/impl/ipc/router.py:664](../../../src/hyprial/daemon/impl/ipc/router.py#L664) |
| `agent.create` | DR-18 | `agent`, `init`, `squire` | [daemon/impl/ipc/router.py:680](../../../src/hyprial/daemon/impl/ipc/router.py#L680) |
| `agent.destroy` | DR-18 | `agent`, `transfer` | [daemon/impl/application/actors/agents_admin/registry_ops.py:803](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L803) |
| `agent.destroy.preview` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:786](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L786) |
| `agent.ensure-home` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:520](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L520) |
| `agent.get` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:753](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L753) |
| `agent.grant` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:107](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L107) |
| `agent.grants` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:107](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L107) |
| `agent.home-census` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:540](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L540) |
| `agent.host-invite` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:295](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L295) |
| `agent.list` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:470](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L470) |
| `agent.migrate.execute` | DR-18 | `agent` | [identity/impl/agents/migration/actor.py:441](../../../src/hyprial/identity/impl/agents/migration/actor.py#L441) |
| `agent.migrate.preflight` | DR-18 | `agent` | [identity/impl/agents/migration/actor.py:405](../../../src/hyprial/identity/impl/agents/migration/actor.py#L405) |
| `agent.migrate.rollback` | DR-18 | `agent` | [identity/impl/agents/migration/actor.py:443](../../../src/hyprial/identity/impl/agents/migration/actor.py#L443) |
| `agent.provider.default` | DR-18 | `agent` | [daemon/impl/ipc/router.py:671](../../../src/hyprial/daemon/impl/ipc/router.py#L671) |
| `agent.provider.login` | DR-18 | `agent` | [daemon/impl/ipc/router.py:671](../../../src/hyprial/daemon/impl/ipc/router.py#L671) |
| `agent.provider.logout` | DR-18 | `agent` | [daemon/impl/ipc/router.py:671](../../../src/hyprial/daemon/impl/ipc/router.py#L671) |
| `agent.readiness` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:626](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L626) |
| `agent.resolve` | DR-18 | — | [daemon/impl/application/actors/agents_admin/registry_ops.py:762](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L762) |
| `agent.restore-policy` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:628](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L628) |
| `agent.restore-threshold` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:628](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L628) |
| `agent.resume.restore` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:61](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L61) |
| `agent.revoke` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:107](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L107) |
| `agent.runtime-context` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:645](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L645) |
| `agent.runtime-launch.acquire` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:697](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L697) |
| `agent.runtime-launch.custody` | DR-18 | — | [daemon/impl/application/actors/agents_admin/registry_ops.py:716](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L716) |
| `agent.runtime-launch.release` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:737](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L737) |
| `agent.secret.grant` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:193](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L193) |
| `agent.secret.list` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:251](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L251) |
| `agent.secret.provider-write` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:169](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L169) |
| `agent.secret.revoke` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:271](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L271) |
| `agent.start.preflight` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:318](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L318) |
| `agent.unblock` | DR-18 | `agent` | [daemon/impl/application/actors/agents_admin/registry_ops.py:636](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L636) |
| `autoupdate.notify` | DR-25 | — | [daemon/impl/ipc/router.py:470](../../../src/hyprial/daemon/impl/ipc/router.py#L470) |
| `autoupdate.status` | DR-25 | `upgrade` | [daemon/impl/ipc/router.py:466](../../../src/hyprial/daemon/impl/ipc/router.py#L466) |
| `autoupdate.trigger` | DR-25 | `upgrade` | [daemon/impl/ipc/router.py:468](../../../src/hyprial/daemon/impl/ipc/router.py#L468) |
| `dispatch.matrix.resolve` | DR-29 | `agent` | [daemon/impl/ipc/router.py:577](../../../src/hyprial/daemon/impl/ipc/router.py#L577) |
| `down` | DR-18 | `agent` | [daemon/impl/ipc/router.py:627](../../../src/hyprial/daemon/impl/ipc/router.py#L627) |
| `hosts` | DR-07 | `host` | [daemon/impl/ipc/router.py:521](../../../src/hyprial/daemon/impl/ipc/router.py#L521) |
| `identity.binding.publish` | DR-08 | `user` | [daemon/impl/ipc/router.py:483](../../../src/hyprial/daemon/impl/ipc/router.py#L483) |
| `identity.binding.withdraw` | DR-08 | `user` | [daemon/impl/ipc/router.py:483](../../../src/hyprial/daemon/impl/ipc/router.py#L483) |
| `identity.bindings.list` | DR-08 | `init`, `squire`, `user` | [daemon/impl/identity/ipc.py:118](../../../src/hyprial/daemon/impl/identity/ipc.py#L118) |
| `identity.override.clear` | DR-08 | `user` | [daemon/impl/identity/ipc.py:177](../../../src/hyprial/daemon/impl/identity/ipc.py#L177) |
| `identity.override.set` | DR-08 | `init`, `squire`, `user` | [daemon/impl/identity/ipc.py:148](../../../src/hyprial/daemon/impl/identity/ipc.py#L148) |
| `identity.resolve` | DR-08 | `user` | [daemon/impl/identity/ipc.py:109](../../../src/hyprial/daemon/impl/identity/ipc.py#L109) |
| `identity.users.add` | DR-08 | `user` | [daemon/impl/identity/ipc.py:204](../../../src/hyprial/daemon/impl/identity/ipc.py#L204) |
| `identity.users.list` | DR-08 | `init`, `squire`, `user` | [daemon/impl/identity/ipc.py:198](../../../src/hyprial/daemon/impl/identity/ipc.py#L198) |
| `identity.whoami` | DR-08 | — | [daemon/impl/ipc/router.py:476](../../../src/hyprial/daemon/impl/ipc/router.py#L476) |
| `lifecycle.resume.preflight` | DR-18 | `agent` | [daemon/impl/ipc/router.py:621](../../../src/hyprial/daemon/impl/ipc/router.py#L621) |
| `lifecycle.start` | DR-18 | `agent`, `squire` | [daemon/impl/ipc/router.py:621](../../../src/hyprial/daemon/impl/ipc/router.py#L621) |
| `lifecycle.start-smolvm` | DR-18 | `agent` | [daemon/impl/ipc/router.py:621](../../../src/hyprial/daemon/impl/ipc/router.py#L621) |
| `link.add` | DR-05 | `link` | [daemon/impl/ipc/router.py:426](../../../src/hyprial/daemon/impl/ipc/router.py#L426) |
| `link.drop` | DR-05 | `link` | [daemon/impl/ipc/router.py:426](../../../src/hyprial/daemon/impl/ipc/router.py#L426) |
| `link.list` | DR-05 | `link` | [daemon/impl/ipc/router.py:426](../../../src/hyprial/daemon/impl/ipc/router.py#L426) |
| `link.stats` | DR-05 | `link` | [daemon/impl/ipc/router.py:426](../../../src/hyprial/daemon/impl/ipc/router.py#L426) |
| `management.adapter.remove` | DR-19 | `adapter` | [daemon/impl/ipc/router.py:440](../../../src/hyprial/daemon/impl/ipc/router.py#L440) |
| `management.squire.ensure` | DR-20 | `init`, `squire` | [daemon/impl/ipc/router.py:438](../../../src/hyprial/daemon/impl/ipc/router.py#L438) |
| `message.ack` | DR-13 | `message` | [daemon/impl/ipc/router.py:611](../../../src/hyprial/daemon/impl/ipc/router.py#L611) |
| `message.history` | DR-13 | — | [daemon/impl/ipc/router.py:541](../../../src/hyprial/daemon/impl/ipc/router.py#L541) |
| `message.pending.batch` | DR-13 | `message` | [daemon/impl/ipc/router.py:545](../../../src/hyprial/daemon/impl/ipc/router.py#L545) |
| `message.pending.list` | DR-13 | — | [daemon/impl/ipc/router.py:543](../../../src/hyprial/daemon/impl/ipc/router.py#L543) |
| `message.pending.wait` | DR-13 | — | [daemon/impl/ipc/router.py:547](../../../src/hyprial/daemon/impl/ipc/router.py#L547) |
| `message.query` | DR-13 | `message` | [daemon/impl/ipc/router.py:539](../../../src/hyprial/daemon/impl/ipc/router.py#L539) |
| `message.read_body` | DR-13 | `message` | [daemon/impl/ipc/router.py:613](../../../src/hyprial/daemon/impl/ipc/router.py#L613) |
| `message.reply` | DR-13 | `message` | [daemon/impl/ipc/router.py:607](../../../src/hyprial/daemon/impl/ipc/router.py#L607) |
| `message.send` | DR-13 | `fs`, `message` | [daemon/impl/ipc/router.py:605](../../../src/hyprial/daemon/impl/ipc/router.py#L605) |
| `message.settle_hint` | DR-13 | — | [daemon/impl/ipc/router.py:615](../../../src/hyprial/daemon/impl/ipc/router.py#L615) |
| `message.status` | DR-13 | `message` | [daemon/impl/ipc/router.py:609](../../../src/hyprial/daemon/impl/ipc/router.py#L609) |
| `network.expose` | DR-06 | `service` | [daemon/impl/ipc/router.py:444](../../../src/hyprial/daemon/impl/ipc/router.py#L444) |
| `network.exposures` | DR-06 | `service` | [daemon/impl/ipc/router.py:442](../../../src/hyprial/daemon/impl/ipc/router.py#L442) |
| `network.peerKey` | DR-06 | `service` | [daemon/impl/ipc/router.py:448](../../../src/hyprial/daemon/impl/ipc/router.py#L448) |
| `network.unexpose` | DR-06 | `service` | [daemon/impl/ipc/router.py:446](../../../src/hyprial/daemon/impl/ipc/router.py#L446) |
| `org.acl.grant` | DR-27 | `org` | [daemon/impl/ipc/router.py:266](../../../src/hyprial/daemon/impl/ipc/router.py#L266) |
| `org.acl.ls` | DR-27 | `org` | [daemon/impl/ipc/router.py:267](../../../src/hyprial/daemon/impl/ipc/router.py#L267) |
| `org.acl.revoke` | DR-27 | `org` | [daemon/impl/ipc/router.py:268](../../../src/hyprial/daemon/impl/ipc/router.py#L268) |
| `org.create` | DR-27 | `org` | [daemon/impl/ipc/router.py:250](../../../src/hyprial/daemon/impl/ipc/router.py#L250) |
| `org.delete` | DR-27 | `org` | [daemon/impl/ipc/router.py:251](../../../src/hyprial/daemon/impl/ipc/router.py#L251) |
| `org.execute` | DR-27 | — | [daemon/impl/ipc/router.py:252](../../../src/hyprial/daemon/impl/ipc/router.py#L252) |
| `org.fetch` | DR-27 | `init`, `login`, `org` | [daemon/impl/ipc/router.py:507](../../../src/hyprial/daemon/impl/ipc/router.py#L507) |
| `org.invite` | DR-27 | `org` | [daemon/impl/ipc/router.py:253](../../../src/hyprial/daemon/impl/ipc/router.py#L253) |
| `org.join` | DR-27 | `init`, `login`, `org` | [daemon/impl/ipc/router.py:254](../../../src/hyprial/daemon/impl/ipc/router.py#L254) |
| `org.leave` | DR-27 | `org` | [daemon/impl/ipc/router.py:255](../../../src/hyprial/daemon/impl/ipc/router.py#L255) |
| `org.list` | DR-27 | `init`, `login`, `org`, `work`, `workflow` | [daemon/impl/ipc/router.py:256](../../../src/hyprial/daemon/impl/ipc/router.py#L256) |
| `org.network` | DR-27 | `doctor`, `org` | [daemon/impl/ipc/router.py:257](../../../src/hyprial/daemon/impl/ipc/router.py#L257) |
| `org.publish` | DR-27 | `org` | [daemon/impl/ipc/router.py:505](../../../src/hyprial/daemon/impl/ipc/router.py#L505) |
| `org.remove` | DR-27 | `org` | [daemon/impl/ipc/router.py:258](../../../src/hyprial/daemon/impl/ipc/router.py#L258) |
| `org.repair` | DR-27 | `org` | [daemon/impl/ipc/router.py:259](../../../src/hyprial/daemon/impl/ipc/router.py#L259) |
| `orgfs.checkout` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:561](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L561) |
| `orgfs.create` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:123](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L123) |
| `orgfs.export` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:436](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L436) |
| `orgfs.history` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:496](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L496) |
| `orgfs.import` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:469](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L469) |
| `orgfs.invite` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:580](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L580) |
| `orgfs.join` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:591](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L591) |
| `orgfs.ls` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:382](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L382) |
| `orgfs.members` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:589](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L589) |
| `orgfs.mkdir` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:483](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L483) |
| `orgfs.move` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:485](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L485) |
| `orgfs.purge` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:542](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L542) |
| `orgfs.purge_plan` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:530](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L530) |
| `orgfs.purge_status` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:548](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L548) |
| `orgfs.read` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:388](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L388) |
| `orgfs.read_at` | DR-14 | — | [daemon/impl/application/messaging/orgfs_bridge.py:508](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L508) |
| `orgfs.remove` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:493](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L493) |
| `orgfs.remove_member` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:586](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L586) |
| `orgfs.resolve` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:380](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L380) |
| `orgfs.restore` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:519](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L519) |
| `orgfs.serve` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:555](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L555) |
| `orgfs.space_purge` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:313](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L313) |
| `orgfs.spaces` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:370](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L370) |
| `orgfs.stat` | DR-14 | — | [daemon/impl/application/messaging/orgfs_bridge.py:385](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L385) |
| `orgfs.stat_at` | DR-14 | — | [daemon/impl/application/messaging/orgfs_bridge.py:513](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L513) |
| `orgfs.status` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:528](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L528) |
| `orgfs.trash` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:517](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L517) |
| `orgfs.unban` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:552](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L552) |
| `orgfs.watch` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:570](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L570) |
| `orgfs.write` | DR-14 | `fs` | [daemon/impl/application/messaging/orgfs_bridge.py:407](../../../src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py#L407) |
| `outbox.list` | DR-13 | `message` | [daemon/impl/ipc/router.py:617](../../../src/hyprial/daemon/impl/ipc/router.py#L617) |
| `outbox.prune` | DR-13 | `message` | [daemon/impl/ipc/router.py:619](../../../src/hyprial/daemon/impl/ipc/router.py#L619) |
| `pac.actor.stop` | DR-16 | — | [daemon/impl/ipc/router.py:591](../../../src/hyprial/daemon/impl/ipc/router.py#L591) |
| `pac.flag.reset` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:591](../../../src/hyprial/daemon/impl/ipc/router.py#L591) |
| `pac.flag.set` | DR-16 | — | [daemon/impl/ipc/router.py:591](../../../src/hyprial/daemon/impl/ipc/router.py#L591) |
| `pac.gc.preview` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:599](../../../src/hyprial/daemon/impl/ipc/router.py#L599) |
| `pac.graph.activate` | DR-16 | — | [daemon/impl/ipc/router.py:591](../../../src/hyprial/daemon/impl/ipc/router.py#L591) |
| `pac.graph.close` | DR-16 | — | [daemon/impl/ipc/router.py:591](../../../src/hyprial/daemon/impl/ipc/router.py#L591) |
| `ping` | DR-21 | `daemon`, `doctor`, `init`, `login`, `upgrade` | [daemon/impl/ipc/router.py:352](../../../src/hyprial/daemon/impl/ipc/router.py#L352) |
| `progress.list` | DR-29 | — | [daemon/impl/ipc/router.py:603](../../../src/hyprial/daemon/impl/ipc/router.py#L603) |
| `ps` | DR-21 | `agent`, `daemon`, `doctor`, `init`, `login`, `squire`, `upgrade`, `workflow` | [daemon/impl/ipc/router.py:472](../../../src/hyprial/daemon/impl/ipc/router.py#L472) |
| `routine.add` | DR-17 | `routine` | [daemon/impl/ipc/router.py:561](../../../src/hyprial/daemon/impl/ipc/router.py#L561) |
| `routine.audit` | DR-17 | `doctor` | [daemon/impl/ipc/router.py:567](../../../src/hyprial/daemon/impl/ipc/router.py#L567) |
| `routine.list` | DR-17 | `agent`, `routine` | [daemon/impl/ipc/router.py:563](../../../src/hyprial/daemon/impl/ipc/router.py#L563) |
| `routine.pause` | DR-17 | `routine` | [daemon/impl/ipc/router.py:573](../../../src/hyprial/daemon/impl/ipc/router.py#L573) |
| `routine.remove` | DR-17 | `routine` | [daemon/impl/ipc/router.py:569](../../../src/hyprial/daemon/impl/ipc/router.py#L569) |
| `routine.resume` | DR-17 | `routine` | [daemon/impl/ipc/router.py:575](../../../src/hyprial/daemon/impl/ipc/router.py#L575) |
| `routine.set` | DR-17 | `routine` | [daemon/impl/ipc/router.py:571](../../../src/hyprial/daemon/impl/ipc/router.py#L571) |
| `routine.status` | DR-17 | `routine` | [daemon/impl/ipc/router.py:565](../../../src/hyprial/daemon/impl/ipc/router.py#L565) |
| `service.connect` | DR-06 | `service` | [daemon/impl/ipc/router.py:411](../../../src/hyprial/daemon/impl/ipc/router.py#L411) |
| `service.disconnect` | DR-06 | `service` | [daemon/impl/ipc/router.py:411](../../../src/hyprial/daemon/impl/ipc/router.py#L411) |
| `service.guide` | DR-06 | `service` | [daemon/impl/ipc/router.py:411](../../../src/hyprial/daemon/impl/ipc/router.py#L411) |
| `service.list` | DR-06 | `service` | [daemon/impl/ipc/router.py:411](../../../src/hyprial/daemon/impl/ipc/router.py#L411) |
| `session.heartbeat` | DR-29 | — | [daemon/impl/ipc/router.py:535](../../../src/hyprial/daemon/impl/ipc/router.py#L535) |
| `session.refresh` | DR-29 | — | [daemon/impl/ipc/router.py:533](../../../src/hyprial/daemon/impl/ipc/router.py#L533) |
| `session.register` | DR-29 | `agent` | [daemon/impl/ipc/router.py:523](../../../src/hyprial/daemon/impl/ipc/router.py#L523) |
| `session.register_guarded` | DR-29 | `agent` | [daemon/impl/ipc/router.py:523](../../../src/hyprial/daemon/impl/ipc/router.py#L523) |
| `session.turn.ended` | DR-29 | — | [daemon/impl/ipc/router.py:531](../../../src/hyprial/daemon/impl/ipc/router.py#L531) |
| `session.turn.started` | DR-29 | — | [daemon/impl/ipc/router.py:529](../../../src/hyprial/daemon/impl/ipc/router.py#L529) |
| `session.turn.refresh` | DR-29 | — | [daemon/impl/request_catalog/session.py:1](../../../src/hyprial/daemon/impl/request_catalog/session.py#L1)（MethodSpec 声明，registry 先于 legacy 链） |
| `session.unregister` | DR-29 | `agent` | [daemon/impl/ipc/router.py:537](../../../src/hyprial/daemon/impl/ipc/router.py#L537) |
| `shutdown` | DR-21 | `daemon`, `doctor`, `init`, `login`, `upgrade` | [daemon/impl/ipc/router.py:687](../../../src/hyprial/daemon/impl/ipc/router.py#L687) |
| `targets` | DR-13 | `fs`, `message` | [daemon/impl/ipc/router.py:519](../../../src/hyprial/daemon/impl/ipc/router.py#L519) |
| `top.snapshot` | DR-21 | `daemon` | [daemon/impl/ipc/router.py:474](../../../src/hyprial/daemon/impl/ipc/router.py#L474) |
| `transfer.complete` | DR-28 | — | [daemon/impl/ipc/router.py:645](../../../src/hyprial/daemon/impl/ipc/router.py#L645) |
| `transfer.custody.override` | DR-28 | — | [daemon/impl/ipc/router.py:629](../../../src/hyprial/daemon/impl/ipc/router.py#L629) |
| `transfer.land` | DR-28 | `transfer` | [daemon/impl/ipc/router.py:631](../../../src/hyprial/daemon/impl/ipc/router.py#L631) |
| `transfer.plan` | DR-28 | — | [daemon/impl/ipc/router.py:633](../../../src/hyprial/daemon/impl/ipc/router.py#L633) |
| `transfer.precheck` | DR-28 | `transfer` | [daemon/impl/ipc/router.py:637](../../../src/hyprial/daemon/impl/ipc/router.py#L637) |
| `transfer.quiesce` | DR-28 | — | [daemon/impl/ipc/router.py:635](../../../src/hyprial/daemon/impl/ipc/router.py#L635) |
| `transfer.receive` | DR-28 | `transfer` | [daemon/impl/ipc/router.py:641](../../../src/hyprial/daemon/impl/ipc/router.py#L641) |
| `transfer.recipientKey` | DR-28 | `transfer` | [daemon/impl/ipc/router.py:639](../../../src/hyprial/daemon/impl/ipc/router.py#L639) |
| `transfer.resume` | DR-28 | — | [daemon/impl/ipc/router.py:643](../../../src/hyprial/daemon/impl/ipc/router.py#L643) |
| `upgrade.steps.run` | DR-25 | — | [daemon/impl/ipc/router.py:457](../../../src/hyprial/daemon/impl/ipc/router.py#L457) |
| `upgrade.steps.status` | DR-25 | — | [daemon/impl/ipc/router.py:450](../../../src/hyprial/daemon/impl/ipc/router.py#L450) |
| `work.add` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:844](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L844) |
| `work.check` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:865](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L865) |
| `work.done` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:833](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L833) |
| `work.keywords` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:926](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L926) |
| `work.link` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:909](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L909) |
| `work.ls` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:828](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L828) |
| `work.note` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:880](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L880) |
| `work.repair` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:848](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L848) |
| `work.resolve` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:855](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L855) |
| `work.show` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:825](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L825) |
| `work.status` | DR-15 | `work` | [daemon/impl/application/messaging/work_items.py:887](../../../src/hyprial/daemon/impl/application/messaging/work_items.py#L887) |
| `worker.channel.request` | DR-29 | — | [daemon/impl/ipc/router.py:430](../../../src/hyprial/daemon/impl/ipc/router.py#L430) |
| `workflow.cancel` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.complete` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:589](../../../src/hyprial/daemon/impl/ipc/router.py#L589) |
| `workflow.expansion.plan` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:587](../../../src/hyprial/daemon/impl/ipc/router.py#L587) |
| `workflow.fail` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:589](../../../src/hyprial/daemon/impl/ipc/router.py#L589) |
| `workflow.history.list` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.history.status` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.list` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.node.inspect` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.plan` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:585](../../../src/hyprial/daemon/impl/ipc/router.py#L585) |
| `workflow.remote.current` | DR-16 | — | [daemon/impl/ipc/router.py:579](../../../src/hyprial/daemon/impl/ipc/router.py#L579) |
| `workflow.start` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:583](../../../src/hyprial/daemon/impl/ipc/router.py#L583) |
| `workflow.status` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:601](../../../src/hyprial/daemon/impl/ipc/router.py#L601) |
| `workflow.worker.restart` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:581](../../../src/hyprial/daemon/impl/ipc/router.py#L581) |
| `workflow.worker.stop` | DR-16 | `workflow` | [daemon/impl/ipc/router.py:581](../../../src/hyprial/daemon/impl/ipc/router.py#L581) |

## 审阅时必须保留的区别

- `agent.secret.list` 当前是授权记录面；CLI `agent secret list` 还直接读取凭据条目。
  提案新增 `agent.secret.entries.list`，不复用同名结果而偷偷改变已有方法语义。
- `agent.destroy.preview` 已存在不代表 execute 已绑定它；DR-04/18 要补 operationId。
- `message.read_body` 是 consuming read，`message.query`/pending 观察不是 ACK。
- `worker.channel.request`、session 方法和内部 transfer 阶段必须保留可信调用者/epoch fencing；
  它们有声明不等于可自动挂到公开 HTTP 路由。
- `pac.gc.preview` 在 router 有真实入口；agents_admin 中同名字的旧分支不是另一个公开方法。
- 单个 CLI 命令可对应多个 IPC，多个 CLI 也可复用一个 IPC；二者数量不应相等。

新增方法提案逐项列在 CLI 附录的目标列；不是这 195 个已存在方法的一部分。
预览配对方法及 terminal I/O 等公共协议补充还须同步主设计的声明、语料和任务验收。
完整 runtime schema/错误语料由对应实现 PR 交付；本附录只证明当前命名/路由盘点，
不冒充所有方法的参数和副作用已经由测试验证。
