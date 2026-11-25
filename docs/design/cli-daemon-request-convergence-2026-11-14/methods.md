# 方法逐项清单（WIP，静态盘点）

基线：`533ef27ca42e53230224d55e58ba074f93d0eb15`。175账本名 = 87已有同名声明 + 88需声明既有操作。当前真实registry为138；另51个方法不在旧账本。尚无完整方法删除或异名替换已获等价证明。

| 方法 | 领域owner | 处置 | 真实绑定/旧分派证据 |
| --- | --- | --- | --- |
| `adapter.list` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/__init__.py:88` |
| `adapter.pin` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/pins.py:159` |
| `adapter.pins` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/pins.py:155` |
| `adapter.reload` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/lifecycle.py:87` |
| `adapter.start` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/lifecycle.py:79` |
| `adapter.status` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/__init__.py:92` |
| `adapter.stop` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/lifecycle.py:83` |
| `adapter.unpin` | `daemon.adapters` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/adapter/pins.py:163` |
| `agent.config.rebind` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:78; src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:88` |
| `agent.create` | `daemon.agent` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/lifecycle.py:743` |
| `agent.destroy` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:825` |
| `agent.destroy.preview` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:808` |
| `agent.ensure-home` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:544` |
| `agent.get` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:775` |
| `agent.grant` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:113; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:117` |
| `agent.grants` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:113` |
| `agent.home-census` | `daemon.agent` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/relations.py:898` |
| `agent.host-invite` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:60; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:320` |
| `agent.list` | `daemon.agent` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/__init__.py:238` |
| `agent.migrate.execute` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:111` |
| `agent.migrate.preflight` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:109` |
| `agent.migrate.rollback` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:111; src/hyprial/daemon/impl/application/actors/agents_admin/migration.py:113` |
| `agent.provider.default` | `daemon.agents` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/provider.py:320` |
| `agent.provider.login` | `daemon.agents` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/provider.py:333` |
| `agent.provider.logout` | `daemon.agents` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/provider.py:346` |
| `agent.readiness` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:650` |
| `agent.resolve` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:784` |
| `agent.restore-policy` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:652` |
| `agent.restore-threshold` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/policies/__init__.py:24; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:652` |
| `agent.resume.restore` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:67` |
| `agent.revoke` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:113; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:144` |
| `agent.runtime-context` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:669` |
| `agent.runtime-launch.acquire` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:719` |
| `agent.runtime-launch.custody` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:738` |
| `agent.runtime-launch.release` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:759` |
| `agent.secret.grant` | `daemon.agent.secret` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/setup/__init__.py:17` |
| `agent.secret.list` | `daemon.agent.secret` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/__init__.py:247` |
| `agent.secret.provider-write` | `daemon.agent.secret` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/setup/__init__.py:17` |
| `agent.secret.revoke` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:175; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:296` |
| `agent.start.preflight` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:60; src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:343` |
| `agent.unblock` | `daemon.agent` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:660` |
| `autoupdate.notify` | `daemon.autoupdate` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:461` |
| `autoupdate.status` | `daemon.autoupdate` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/maintenance/read_specs.py:76` |
| `autoupdate.trigger` | `daemon.autoupdate` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:459` |
| `dispatch.matrix.resolve` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:541` |
| `down` | `daemon.lifecycle` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:589` |
| `hosts` | `daemon.status` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/view_specs.py:114` |
| `identity.binding.publish` | `daemon.identity` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/identity/ipc.py:94; src/hyprial/daemon/impl/identity/ipc.py:128` |
| `identity.binding.withdraw` | `daemon.identity` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/identity/ipc.py:94; src/hyprial/daemon/impl/identity/ipc.py:128` |
| `identity.bindings.list` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/__init__.py:36` |
| `identity.override.clear` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/writes.py:13` |
| `identity.override.set` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/writes.py:13` |
| `identity.resolve` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/__init__.py:40` |
| `identity.users.add` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/writes.py:13` |
| `identity.users.list` | `daemon.identity` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/identity/__init__.py:32` |
| `identity.whoami` | `daemon.identity` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:465` |
| `lifecycle.resume.preflight` | `daemon.lifecycle` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/lifecycle_ops.py:598; src/hyprial/daemon/impl/ipc/router.py:583` |
| `lifecycle.start` | `daemon.lifecycle` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:583` |
| `lifecycle.start-smolvm` | `daemon.lifecycle` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/lifecycle_ops.py:599; src/hyprial/daemon/impl/ipc/router.py:583` |
| `link.add` | `daemon.links` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/link.py:381` |
| `link.drop` | `daemon.links` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/link.py:399` |
| `link.list` | `daemon.links` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/link.py:346` |
| `link.stats` | `daemon.links` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/link.py:357` |
| `management.adapter.remove` | `daemon.management` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:431` |
| `management.squire.ensure` | `daemon.management` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:429` |
| `message.ack` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/messaging/visibility.py:455; src/hyprial/daemon/impl/application/messaging/visibility.py:477` |
| `message.history` | `daemon.inbox` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/history.py:78` |
| `message.pending.batch` | `daemon.messaging.inbox` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/batch.py:174` |
| `message.pending.list` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/messaging/visibility.py:455; src/hyprial/daemon/impl/application/messaging/visibility.py:477` |
| `message.pending.wait` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/messaging/visibility.py:455; src/hyprial/daemon/impl/application/messaging/visibility.py:477` |
| `message.query` | `daemon.inbox` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/query.py:76` |
| `message.read_body` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/messaging/visibility.py:455; src/hyprial/daemon/impl/application/messaging/visibility.py:477` |
| `message.reply` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:571` |
| `message.send` | `daemon.inbox` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/send/specs.py:311` |
| `message.settle_hint` | `daemon.message` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/messaging/visibility.py:455; src/hyprial/daemon/impl/application/messaging/visibility.py:477` |
| `message.status` | `daemon.messaging.delivery` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/status.py:181` |
| `network.expose` | `daemon.network` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:435` |
| `network.exposures` | `daemon.network` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:433` |
| `network.peerKey` | `daemon.network` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:439` |
| `network.unexpose` | `daemon.network` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:437` |
| `org.acl.grant` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/acl/specs.py:211` |
| `org.acl.ls` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/acl/specs.py:200` |
| `org.acl.revoke` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/acl/specs.py:222` |
| `org.create` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/__init__.py:693` |
| `org.delete` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/removal_specs.py:366` |
| `org.execute` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/removal_specs.py:333` |
| `org.fetch` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/__init__.py:738` |
| `org.invite` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/__init__.py:704` |
| `org.join` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/__init__.py:715` |
| `org.leave` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/removal_specs.py:344` |
| `org.list` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/__init__.py:727` |
| `org.network` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/network_specs.py:448` |
| `org.publish` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/network_specs.py:470` |
| `org.remove` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/removal_specs.py:355` |
| `org.repair` | `daemon.org` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/network_specs.py:459` |
| `orgfs.checkout` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.create` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.export` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.history` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:248` |
| `orgfs.import` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.invite` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.join` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.ls` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:203` |
| `orgfs.members` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:284` |
| `orgfs.mkdir` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.move` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.purge` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.purge_plan` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.purge_status` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:293` |
| `orgfs.read` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:221` |
| `orgfs.read_at` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:230` |
| `orgfs.remove` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.remove_member` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.resolve` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:194` |
| `orgfs.restore` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.serve` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.space_purge` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.spaces` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:185` |
| `orgfs.stat` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:212` |
| `orgfs.stat_at` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:239` |
| `orgfs.status` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:257` |
| `orgfs.trash` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:266` |
| `orgfs.unban` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `orgfs.watch` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/read_specs.py:275` |
| `orgfs.write` | `daemon.orgfs` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/orgfs/write_specs.py:380` |
| `outbox.list` | `daemon.outbox` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:579` |
| `outbox.prune` | `daemon.outbox` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:581` |
| `pac.actor.stop` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:740; src/hyprial/daemon/impl/application/workflows.py:681` |
| `pac.flag.reset` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:643; src/hyprial/daemon/impl/application/workflows.py:700` |
| `pac.flag.set` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:643; src/hyprial/daemon/impl/application/workflows.py:700` |
| `pac.gc.preview` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:819; src/hyprial/daemon/impl/ipc/router.py:563` |
| `pac.graph.activate` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:734; src/hyprial/daemon/impl/application/workflows.py:677` |
| `pac.graph.close` | `daemon.workflows` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:737; src/hyprial/daemon/impl/application/workflows.py:679` |
| `ping` | `daemon.status` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/onboarding/gui_host.py:295; src/hyprial/daemon/impl/application/onboarding/gui_host.py:273` |
| `progress.list` | `daemon.inbox` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/progress/__init__.py:104` |
| `ps` | `daemon.status` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:463` |
| `routine.add` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:529` |
| `routine.audit` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:531` |
| `routine.list` | `daemon.routine` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/relations.py:778` |
| `routine.pause` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:537; src/hyprial/daemon/impl/ipc/router.py:518` |
| `routine.remove` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:533; src/hyprial/daemon/impl/ipc/router.py:518` |
| `routine.resume` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:539; src/hyprial/daemon/impl/ipc/router.py:518` |
| `routine.set` | `daemon.routine` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:535; src/hyprial/daemon/impl/ipc/router.py:518` |
| `routine.status` | `daemon.routine` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/relations.py:808` |
| `service.connect` | `daemon.service` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:391; src/hyprial/daemon/impl/service_connect/manager.py:264` |
| `service.disconnect` | `daemon.service` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:391` |
| `service.guide` | `daemon.service` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:391; src/hyprial/daemon/impl/service_connect/manager.py:264` |
| `service.list` | `daemon.service` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:391; src/hyprial/daemon/impl/service_connect/manager.py:264` |
| `session.heartbeat` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:508` |
| `session.refresh` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:506` |
| `session.register` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:496` |
| `session.register_guarded` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:496; src/hyprial/daemon/impl/ipc/router.py:499` |
| `session.turn.ended` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:504` |
| `session.turn.started` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:502` |
| `session.unregister` | `daemon.session` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:510` |
| `shutdown` | `daemon.lifecycle` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:610; src/hyprial/daemon/impl/ipc/router.py:370` |
| `targets` | `daemon.messaging.directory` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/message/directory.py:68` |
| `top.snapshot` | `daemon.status` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/org/view_specs.py:128` |
| `upgrade.steps.run` | `daemon.upgrade` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/upgrade/methods.py:42` |
| `upgrade.steps.status` | `daemon.upgrade` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/maintenance/steps.py:58` |
| `worker.channel.request` | `daemon.sessions` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:420` |
| `workflow.cancel` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:372; src/hyprial/daemon/impl/ipc/router.py:565` |
| `workflow.complete` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/dispatch/remote/__init__.py:937; src/hyprial/daemon/impl/ipc/router.py:553` |
| `workflow.expansion.plan` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:551` |
| `workflow.fail` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:242; src/hyprial/daemon/impl/dispatch/remote/__init__.py:937` |
| `workflow.history.list` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:565` |
| `workflow.history.status` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:565` |
| `workflow.list` | `daemon.workflow` | 复用已有声明；审清旧分派 | `src/hyprial/daemon/impl/request_catalog/agent/relations.py:851` |
| `workflow.node.inspect` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/application/workflows.py:381; src/hyprial/daemon/impl/application/workflows.py:374` |
| `workflow.plan` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:549` |
| `workflow.remote.current` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:543` |
| `workflow.start` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:547` |
| `workflow.status` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:565` |
| `workflow.worker.restart` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:545` |
| `workflow.worker.stop` | `daemon.workflow` | 声明现有操作；保留真实实现 | `src/hyprial/daemon/impl/ipc/router.py:545` |

## 七个前缀

前缀不是 MethodSpec，不声明通配符。以下“待删除”指完成精确绑定或保留等价拒绝行为后删除分派条件，不能直接删除权限前置检查。

| 前缀 | 领域 | 尚未声明的字面方法数 | 处置与证据 |
| --- | --- | ---: | --- |
| `agent.` | agent | 24 | 24个未声明agent方法完成精确绑定后移除fallback；公共Agent错误翻译进入同一daemon-owned调用边界。 [src/hyprial/daemon/impl/ipc/router.py:597](../../../src/hyprial/daemon/impl/ipc/router.py#L597) |
| `agent.migrate.` | agent | 3 | agent三migrate及config.rebind仍真实调用migration helper；拆成精确绑定，保留shared migration domain业务。 [src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:111](../../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py#L111) |
| `identity.` | identity | 3 | 当前只剩binding publish/withdraw等未声明名走此路；迁移可信operator/peer_pid装配后移除fallback。 [src/hyprial/daemon/impl/ipc/router.py:467](../../../src/hyprial/daemon/impl/ipc/router.py#L467) |
| `identity.binding.` | identity | 2 | 该表达式是identity fallback中的权限选择，不是独立方法家族分发；与2个binding方法一起迁移，不能先删。 [src/hyprial/daemon/impl/ipc/router.py:486](../../../src/hyprial/daemon/impl/ipc/router.py#L486) |
| `orgfs.` | orgfs | 0 | 30个ledger方法全部已声明；此处只是未知orgfs名称METHOD_NOT_FOUND拒绝，不是残余业务路由。可由统一closed-family refusal替代，须保留未知名错误。 [src/hyprial/daemon/impl/ipc/router.py:422](../../../src/hyprial/daemon/impl/ipc/router.py#L422) |
| `routine.` | routine | 6 | 此处是6个未声明routine方法前置caller/owner校验，非完整分派；移入共有daemon授权边界并由精确绑定调用。 [src/hyprial/daemon/impl/ipc/router.py:516](../../../src/hyprial/daemon/impl/ipc/router.py#L516) |
| `workflow.history.` | workflow | 2 | _ipc_workflow_status_group内部用于选择历史查询/授权语义；两history方法声明时变成精确handler/共有history业务入口，不复制可见性规则。 [src/hyprial/daemon/impl/application/workflows.py:355](../../../src/hyprial/daemon/impl/application/workflows.py#L355) |

完整 metadata、真实 handler、分派表达式、registry-only 51 项见 [methods.json](methods.json)。参数/结果/错误/权限等价仍须每批行为验证；87 个同名声明不是已获删除许可。
