# UUID 寻址：名字→URI 铸造点冻结清单

- 推导时刻：epoch 1792768646
- 冻结基底：HEAD `d05d32b9f` (`d05d32b9fdb8ab81b39f02ecce49696673bc6b29`)
- 推导者：hyprial-cli-developer（研发部）
- 状态：**P2 第一步的输入**；此后新增/删除必须出新 rev（⛔ 原地改）

## 推导命令（可重放）

```
git grep -n -E 'canonical_agent_uri\(|_canonical_harness_uri\(|canonical_user_uri\(' \
  d05d32b9fdb8ab81b39f02ecce49696673bc6b29 -- 'src/**/*.py'
```

## 计数口径

⚠️ 只数**定义处之外**的调用点；⚠️ 同行多调用按一行计（⛔ 用 grep -c 当次数）。

### agent-name→agent-uri

- 命中行数：**34**（定义处 1 行已排除）

| file:line | 行 sha16 | 行内容（截断） |
|---|---|---|
| `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:837` | `f28a57b5d0f81ba0` | `actor = canonical_agent_uri(` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:95` | `4eb1d7c905bb185f` | `return canonical_agent_uri(owner, self.node_id, name)` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:163` | `1f6a3ce000c1a2e4` | `canonical = canonical_agent_uri(self.owner, self.node_id, alias)` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:350` | `800d981c7e199676` | `# canonical_agent_uri(owner, node, name) always ends ":<name>", so` |
| `src/hyprial/daemon/impl/application/actors/routines.py:454` | `0a5209570a82f177` | `actor = canonical_agent_uri(self.owner, self.node_id, item.name)` |
| `src/hyprial/daemon/impl/application/actors/routines.py:493` | `ee298fc5bc2cb99d` | `document["produces"] = canonical_agent_uri(self.owner, self.node_id, actor_name)` |
| `src/hyprial/daemon/impl/application/messaging/delivery/sends.py:720` | `24eb2ef07e87eab6` | `sender = canonical_agent_uri(self.owner, self.node_id, "squire")` |
| `src/hyprial/daemon/impl/application/messaging/delivery/status_queries.py:152` | `bf6f07c8f55b069b` | `keys.append(canonical_agent_uri(self.owner, self.node_id, sender))` |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py:951` | `e4cadbe53285b3a7` | `return (actor, canonical_agent_uri(self.owner, self.node_id, raw))` |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:260` | `3f92f2c2a0ef0e5e` | `sender=canonical_agent_uri(self.owner, self.node_id, "squire"),` |
| `src/hyprial/daemon/impl/application/messaging/status/views.py:535` | `b080f5ccd1f42487` | `else canonical_agent_uri(` |
| `src/hyprial/daemon/impl/application/messaging/status/views.py:962` | `e963bc40e2a0bc5c` | `current = canonical_agent_uri(self.owner, self.node_id, actor)` |
| `src/hyprial/daemon/impl/application/messaging/visibility.py:464` | `153b84427ba90183` | `return canonical_agent_uri(self.owner, self.node_id, actor)` |
| `src/hyprial/daemon/impl/application/shutdown.py:904` | `b080f5ccd1f42487` | `else canonical_agent_uri(` |
| `src/hyprial/daemon/impl/application/wiring/transport.py:607` | `3d536cd73da26dac` | `callback_actor=canonical_agent_uri(` |
| `src/hyprial/daemon/impl/composition/links.py:915` | `af4fb52f5d0dcbd1` | `else canonical_agent_uri(app.owner, app.node_id, str(status.get("name")))` |
| `src/hyprial/daemon/impl/composition/views.py:139` | `d6424cf2d03b56f4` | `return canonical_agent_uri(self.owner, self.machine, name)` |
| `src/hyprial/daemon/impl/dispatch/identity.py:17` | `584c817bdb680e9a` | `return canonical_agent_uri(owner, node_id, DISPATCH_SERVICE_ACTOR_NAME)` |
| `src/hyprial/daemon/impl/operations/top.py:340` | `95dfc794f6f74399` | `else canonical_agent_uri(owner, node_id, str(status.get("name")))` |
| `src/hyprial/daemon/impl/operations/top.py:488` | `95dfc794f6f74399` | `else canonical_agent_uri(owner, node_id, str(status.get("name")))` |
| `src/hyprial/daemon/impl/pac/contracts/roster.py:69` | `268d6ccea80c348a` | `"owner": canonical_agent_uri(` |
| `src/hyprial/daemon/impl/pac/workflows/graphs.py:211` | `c19af80fae0cbee4` | `"actor_uri": canonical_agent_uri(` |
| `src/hyprial/daemon/impl/pac/workflows/graphs.py:291` | `15a7e4518ade29e8` | `owner = canonical_agent_uri(local_owner, machine, "pac-expansion")` |
| `src/hyprial/daemon/impl/squire/setup.py:119` | `bdd1de171b38cd87` | `return canonical_agent_uri(self.owner, self.machine, "squire")` |
| `src/hyprial/daemon/impl/status/__init__.py:78` | `02c2f63ad9d343bc` | `actor = registered_by_name.get(name) or canonical_agent_uri(owner, node_id, name` |
| `src/hyprial/daemon/impl/status/__init__.py:101` | `cc8d6dae012ab2a0` | `actor = canonical_agent_uri(owner, node_id, actor)` |
| `src/hyprial/identity/impl/agents/actor/_core.py:741` | `9e25d43c905079fb` | `return canonical_agent_uri(self._registry.owner, self._registry.machine, name)` |
| `src/hyprial/identity/impl/agents/registry/_base.py:313` | `22f39903938fda23` | `uri = _uri().canonical_agent_uri(owner, machine, actor)` |
| `src/hyprial/identity/impl/agents/registry/_core.py:194` | `623755356256d8a9` | `return _uri().canonical_agent_uri(self.owner, self.machine, name)` |
| `src/hyprial/identity/impl/agents/registry/_home_create.py:76` | `a061cf3315b8ff18` | `uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),` |
| `src/hyprial/identity/impl/agents/registry/_home_create.py:106` | `a061cf3315b8ff18` | `uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),` |
| `src/hyprial/identity/impl/agents/registry/_home_create.py:281` | `8e010614538581d0` | `uri=_uri().canonical_agent_uri(` |
| `src/hyprial/identity/impl/agents/registry/_home_create.py:304` | `8e010614538581d0` | `uri=_uri().canonical_agent_uri(` |
| `src/hyprial/shell/impl/cli/commands/agent/runtime/interactive.py:107` | `5035b2bad1aaf09d` | `return canonical_agent_uri(owner, node_id, name)` |

### harness-name→agent-uri

- 命中行数：**21**（定义处 1 行已排除）

| file:line | 行 sha16 | 行内容（截断） |
|---|---|---|
| `src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py:149` | `bd7a8cdef79a024b` | `if self._canonical_harness_uri(spec.name, spec) != actor:` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:146` | `e1954cdf30ba899a` | `candidates.add(self._canonical_harness_uri(spec.name, spec))` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:304` | `32b3edacd99f6de0` | `and self._canonical_harness_uri(spec.name, spec) == target` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:356` | `065c535a6dd6d6ff` | `self._canonical_harness_uri(name, desired=desired) == actor` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:378` | `5db0ce06d3cf3d55` | `if self._canonical_harness_uri(spec.name, spec) == actor:` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:398` | `addddc5479908cff` | `self._canonical_harness_uri(spec.name, spec),` |
| `src/hyprial/daemon/impl/application/actors/agents_registry.py:403` | `addddc5479908cff` | `self._canonical_harness_uri(spec.name, spec),` |
| `src/hyprial/daemon/impl/application/actors/lifecycle_ops.py:206` | `f29f19a44ab22ea7` | `actor = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/actors/lifecycle_ops.py:565` | `f92eb02d1a619fd1` | `actor_uri = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/actors/transfer.py:161` | `f92eb02d1a619fd1` | `actor_uri = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/actors/transfer.py:601` | `f92eb02d1a619fd1` | `actor_uri = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/actors/transfer.py:887` | `f92eb02d1a619fd1` | `actor_uri = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/messaging/delivery/sends.py:287` | `c9a90b6135b1877d` | `recipient = self._canonical_harness_uri(agent, spec, desired)` |
| `src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py:642` | `5db0ce06d3cf3d55` | `if self._canonical_harness_uri(spec.name, spec) == actor:` |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:717` | `5db0ce06d3cf3d55` | `if self._canonical_harness_uri(spec.name, spec) == actor:` |
| `src/hyprial/daemon/impl/application/messaging/status/snapshots.py:107` | `709f848e15ee4a80` | `self._canonical_harness_uri(name, specs_by_name.get(name)), running` |
| `src/hyprial/daemon/impl/application/messaging/status/snapshots.py:117` | `addddc5479908cff` | `self._canonical_harness_uri(spec.name, spec),` |
| `src/hyprial/daemon/impl/application/messaging/status/views.py:187` | `6a6f1280216e7cb3` | `self._canonical_harness_uri(str(row["name"]), spec)` |
| `src/hyprial/daemon/impl/application/messaging/work_items.py:114` | `f29f19a44ab22ea7` | `actor = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/application/wiring/transport.py:677` | `f29f19a44ab22ea7` | `actor = self._canonical_harness_uri(spec.name, spec)` |
| `src/hyprial/daemon/impl/pac/actors/daemon.py:98` | `5238bb8104c3408b` | `actor = self.application._canonical_harness_uri(actor_name, spec)` |

### owner-name→user-uri

- 命中行数：**48**（定义处 1 行已排除）

| file:line | 行 sha16 | 行内容（截断） |
|---|---|---|
| `src/hyprial/biz/impl/workflow/identity.py:28` | `673630be5cc2e8f5` | `return canonical_user_uri(resolve_node_owner(hyprial_home=home))` |
| `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:125` | `fab09448d9661bfe` | `canonical_user_uri(self.owner), revision,` |
| `src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py:147` | `2d859981dac5442e` | `canonical_user_uri(self.owner),` |
| `src/hyprial/daemon/impl/application/actors/routines.py:567` | `5d8c426b795b8a79` | `or routine_caller == canonical_user_uri(self.owner)` |
| `src/hyprial/daemon/impl/application/actors/transfer.py:395` | `1a7d732b13bf3f17` | `granted_by=canonical_user_uri(self.owner),` |
| `src/hyprial/daemon/impl/application/actors/transfer_p2.py:367` | `3617b2a7f3e341ce` | `if canonical_user_uri(principal) != canonical_user_uri(self.owner):` |
| `src/hyprial/daemon/impl/application/messaging/delivery/sends.py:178` | `7e76d845e404c9cf` | `sender=sender or canonical_user_uri(self.owner),` |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:219` | `3ee95f532a7f4e3a` | `else canonical_user_uri(self.owner)` |
| `src/hyprial/daemon/impl/application/messaging/orgfs_bridge.py:639` | `3ee95f532a7f4e3a` | `else canonical_user_uri(self.owner)` |
| `src/hyprial/daemon/impl/application/messaging/visibility.py:284` | `4314baf2fddc3c44` | `if normalized == canonical_user_uri(self.owner):` |
| `src/hyprial/daemon/impl/application/messaging/work/conflicts.py:51` | `79b97d7c22bc44b7` | `principal = canonical_user_uri(agent[0])` |
| `src/hyprial/daemon/impl/application/shutdown.py:887` | `74da81aaad3da8a6` | `addresses.add(canonical_user_uri(self.owner))` |
| `src/hyprial/daemon/impl/application/shutdown.py:891` | `5cb94c2a6b1eb288` | `addresses.add(canonical_user_uri(profile.owner_key))` |
| `src/hyprial/daemon/impl/application/workflows.py:326` | `bb4f55f79526c11e` | `viewer = None if caller == canonical_user_uri(self.owner) else caller` |
| `src/hyprial/daemon/impl/application/workflows.py:339` | `bec894ee127830f8` | `viewer=None if all_callers or caller == canonical_user_uri(self.owner) else call` |
| `src/hyprial/daemon/impl/application/workflows.py:348` | `49f43600b32a2192` | `if caller != canonical_user_uri(self.owner) and caller != result["sender"] and n` |
| `src/hyprial/daemon/impl/application/workflows.py:355` | `e7ca9882cb63b138` | `if caller not in (canonical_user_uri(self.owner), result["sender"], node["owner"` |
| `src/hyprial/daemon/impl/application/workflows.py:409` | `4248e45d8240f54f` | `if node.owner != canonical_user_uri(self.owner):` |
| `src/hyprial/daemon/impl/application/workflows.py:442` | `811a5a16421bc751` | `if actor == canonical_user_uri(self.owner) and "sessionRef" not in params:` |
| `src/hyprial/daemon/impl/composition/links.py:294` | `6029d1f6600a9ae7` | `return canonical_user_uri(self._bare_owner)` |
| `src/hyprial/daemon/impl/composition/links.py:295` | `d4f3a418f5be5431` | `return canonical_user_uri(parsed[0])` |
| `src/hyprial/daemon/impl/ipc/router.py:554` | `594f50a76731b792` | `if routine_caller not in (canonical_user_uri(self.owner), current["owner"], curr` |
| `src/hyprial/daemon/impl/links/model.py:95` | `d4f3a418f5be5431` | `return canonical_user_uri(parsed[0])` |
| `src/hyprial/daemon/impl/links/service/gate.py:631` | `fd5ed93dfcc988d1` | `return parsed is not None and canonical_user_uri(parsed[0]) in members` |
| `src/hyprial/daemon/impl/org/network/access/snapshot.py:203` | `d44809dacf515ae7` | `owned=space.owner == canonical_user_uri(service._owner),` |
| `src/hyprial/daemon/impl/org/network/casdoor/membership.py:207` | `c8c39738491737d5` | `candidates = [user] if user.startswith("user:") else [user, canonical_user_uri(u` |
| `src/hyprial/daemon/impl/org/network/casdoor/membership.py:229` | `397d3652ce1c537c` | `me = canonical_user_uri(self._owner)` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:119` | `77669f8178aba904` | `return canonical_user_uri(user.split("/", 1)[1] if "/" in user else user)` |
| `src/hyprial/daemon/impl/org/network/casdoor/sync.py:409` | `d0b4182c86852ddc` | `owner = canonical_user_uri(service._owner)` |
| `src/hyprial/daemon/impl/org/network/create/recovery.py:73` | `464f0942787200e2` | `principal = canonical_user_uri(owner)` |
| `src/hyprial/daemon/impl/org/network/create/recovery.py:216` | `36e1315ffe5c68d3` | `repairable = space.owner == canonical_user_uri(owner) and (` |
| `src/hyprial/daemon/impl/org/network/create/recovery.py:220` | `c0e27c436b2a3b38` | `and intent.get("owner") == canonical_user_uri(owner)` |
| `src/hyprial/daemon/impl/org/network/directory.py:885` | `c6c41b268e606f76` | `canonical_user_uri(user.removeprefix("user:"))` |
| `src/hyprial/daemon/impl/org/network/ipc.py:316` | `bf3ac7d1ea7a1bc4` | `else canonical_user_uri(app.owner)` |
| `src/hyprial/daemon/impl/org/network/service.py:96` | `6f0b5c9b7e15d5ea` | `return canonical_user_uri(user)` |
| `src/hyprial/daemon/impl/org/network/service.py:457` | `50278f81e8871436` | `require_space_owner(canonical_user_uri(self._owner), space)` |
| `src/hyprial/daemon/impl/org/orgfs_migration.py:267` | `a958364508fce74c` | `users.append(canonical_user_uri(node.name[:-3]))` |
| `src/hyprial/daemon/impl/orgfs/webserver/server.py:268` | `314996cf89b6a694` | `if canonical_user_uri(identity.login_name or "") not in users:` |
| `src/hyprial/daemon/impl/pac/contracts/principals.py:156` | `e470ec37d04263e4` | `uri=canonical_user_uri(str(profile["owner"])),` |
| `src/hyprial/daemon/impl/pac/views/work_items.py:175` | `b3417ce8b243ac3b` | `owner = canonical_user_uri(owner)` |
| `src/hyprial/daemon/impl/pac/workflows/runtime/operations.py:357` | `0ae3c654394219c8` | `projected["owner"] == canonical_user_uri(self.owner)` |
| `src/hyprial/daemon/impl/squire/addressing.py:105` | `14bbbb51dc902eaf` | `return canonical_user_uri(self.owner)` |
| `src/hyprial/daemon/impl/transfer/p2/orchestration/receive.py:221` | `9473d66cfc7c2186` | `granted_by=canonical_user_uri(owner),` |
| `src/hyprial/identity/impl/agents/registry/_records.py:651` | `cf1f56a9f48df36f` | `by=canonical_user_uri(self.owner), note="ownerAsserted=host",` |
| `src/hyprial/identity/impl/agents/registry/_records.py:657` | `f322119f5d48348c` | `if by != canonical_user_uri(self.owner) or note != "ownerAsserted=host":` |
| `src/hyprial/identity/impl/org_directory/models.py:237` | `c640a26139d8c6f9` | `return canonical_user_uri(bare)` |
| `src/hyprial/shell/impl/cli/commands/common/support.py:690` | `19ec2586a1977f23` | `f"{canonical_user_uri(owner)} yet. Set it up with: hyprial squire setup"` |
| `src/hyprial/shell/impl/cli/commands/common/support.py:839` | `673630be5cc2e8f5` | `return canonical_user_uri(resolve_node_owner(hyprial_home=home))` |

## 与旧数「33 处」的关系

* 侦察（2026-10-15）报 33 处，用的是当时工作区上**更宽的谓词**（含解析/比较点）。
* 本清单在 `d05d32b9f` 上按**函数调用**重推：agent-name **34** · harness-name **21** · owner-name **48**。
⇒ 两数不同**不是代码变了**，是**谓词变了**；以本表为准，⛔ 再引用「33 处」而不给谓词。

