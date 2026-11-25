# Org 成员模型：一个 home 多个 org，通信只由 org 决定（0.5.5.3 设计）

状态：**已裁定**（2026-10-29 起草；2026-10-31 Allen 裁定 §8 全部按推荐，见 §8；GUI 设计以本文“GUI 需要的面”为输入）
目标版本：release/v0.5.5.3
代码基线：dev `e228d4ae4`（文中 `file:line` 均指该树）

## 0. 结论

1. 一个 hyprial home 同时属于多个 org。`org use <org>` 设定当前 org，作为 org 类命令的默认目标；登录后不再自动接受所有邀请，要逐条确认。
2. 每个 agent 都有显式的 org 归属，至少属于一个，归属记在 OrgFS。每个用户至少有一个个人 org，squire 和 user-proxy 住在里面，preferredReceiver 改为个人 org 里的一条 OrgFS 记录。
3. “跨 org 通信”不再是一种机制。它的含义是一个 agent 或用户同时是多个 org 的成员。两方能否通信只看双方共同所在、且配置允许的 org。
4. 所有基于 owner 的捷径一律删除，见 §5 的清单。这包括 agent↔自己主人的免检和“主人属于任一 org 即放行”的 bypass。
5. Casdoor group 是 org 成员的唯一事实源。成员变更只从 org 侧发起（Casdoor → OrgFS），节点不反向写 Casdoor 的成员关系。
6. 离线设备的重试设预算。预算用完就停止，直到有明确的连接需求（发消息、读文档、用户手动）才重试。

## 1. Allen 的裁定（原话，2026-10-29 飞书）

> 4. 离线设备在当前的网络设计下非常常见,对离线设备重试导致CPU占用很高看起来是设计的问题。理论上,连接设备在一定重试预算后停止重试,直到有明确的连接需求后再重新尝试连接
>
> 5. “一个 home 里放多个 org” 是现在的决策决定,请更新,org use等建议很好
>
> 6. agent 应该有自己的 org 归属,每个agent至少属于一个org;每个用户至少有一个org(个人组织空间),所谓的“跨org通信”,实际上应该是一个agent/user同时在多个org中
>
> 7. “同一主人的 agent 是否默认互通” : 不应该,能否通信的配置完全放在org中,不要再搭建一套基于owner的机制,如果已经有了,规划尽快删除

研发负责人对 Allen 同日裁定的转述（非原话）：Casdoor 是 org 成员关系的唯一事实源，操作只从 org 侧发起。

## 2. 现状（代码事实）

| 方面 | 现状 | 依据 |
|---|---|---|
| 一个 home 多个 org | 已经支持。`_orgs()` 取 store 里的 org，去掉已离开和已删除的 | `src/hyprial/daemon/impl/org/network/service.py:701-709` |
| 当前 org | 没有这个概念。多 org 时 `work` 命令直接报 `WORK_ORG_REQUIRED` | `src/hyprial/shell/impl/cli/commands/work/commands.py:95-100` |
| 登录后的邀请 | `login` 以 `accept_all=True` 静默加入全部邀请 | `src/hyprial/shell/impl/cli/commands/login/flow.py:696-698`；`src/hyprial/shell/impl/invites/pending.py:160-162` |
| `org pending` | 有 `--accept-all`，没有它时逐条确认 | `src/hyprial/shell/impl/cli/commands/org/commands.py:632` |
| `org delete` | 必然失败：权限检查后无条件抛出 `LAST_AUTHORITY_REFUSED` | `src/hyprial/daemon/impl/org/network/service.py:416-420` |
| agent 的 org 归属 | agent 记录只有 owner/machine/uri/hosted_by，没有 org 字段。归属反过来记在 org-context 的 members/residents 中 | `src/hyprial/identity/impl/agents/registry/_records.py:164,230`；`src/hyprial/daemon/impl/org/document.py:56,217,253` |
| squire 接收机 | `preferredReceiver` / `squireChannel` 存在各 daemon 本地的用户档案里，不在节点间同步 | `src/hyprial/identity/impl/users/profile.py:21-27,227-283` |
| 链接紧急开关 | `HYPRIAL_LINKS_BYPASS` / `links.bypass` 默认为开 | `src/hyprial/daemon/impl/composition/links.py:98-101,134-156` |
| bypass 下的入站判定 | 远端 agent 发件人的主人只要是本节点读到的任一链接空间的成员即放行 | `src/hyprial/daemon/impl/links/service/gate.py:7-11,152-175`；`src/hyprial/daemon/impl/links/store.py:709-722` |
| owner 对免检 | agent↔其主人不经链接机制，也不写 OrgFS | `src/hyprial/daemon/impl/links/model.py:11,98-105,181,271,286`；`src/hyprial/daemon/impl/links/policy.py:160-167` |
| `user:` 目标 | 所有 `user:` 目标（主人和他人）都免链接检查 | `src/hyprial/daemon/impl/links/policy.py:160-165` |
| 设备准入（Tailcat） | 已按 org 目录推导允许集，失败即关闭（本设计不改） | `docs/design/org-device-inbound-admission.md` |
| Casdoor → OrgFS | owner 节点上的成员 actor：webhook 或本地命令逐个处理用户，加定期全量 backstop；带断路器；owner 永不被移除 | `src/hyprial/daemon/impl/org/network/casdoor/sync.py:1-26` |
| 离线 supplier 重试 | 指数 hold，有上限但永不停止。实测 hq 对一台离线机连续失败 66–67 次 | `src/hyprial/daemon/impl/orgfs/network/supplier_health.py:97,121-126` |

结论：多 org 的存储已经有了。缺的是“当前 org”、agent 自身的归属和个人 org；通信判定则仍混着 owner 规则。

## 3. 模型

### 3.1 实体

- **org**：一个 Casdoor group 对应一个 OrgFS 空间（目录空间加数据空间）。`kind ∈ {work, personal}`。
- **personal org**：每个用户自动有一个，名字是 `personal-<userKey>`，用户本人是唯一成员。用户的每台机器都是它的 host。
- **membership**：`(principal, org, role)`。principal 是 `user:<u>` 或 `agent:<owner>:<node>:<name>`。用户成员关系以 Casdoor 为准；agent 成员关系写在 org 的 OrgFS 目录里。
- **current org**：每个 home 一个本地设置，只影响 CLI/GUI 的默认目标，不影响通信判定。

### 3.2 规则

- R1：每个 agent 至少属于一个 org。没有归属的 agent 不能被创建；把最后一个 org 移除会被拒绝（`AGENT_LAST_ORG`）。
- R2：创建 agent 时，若没有显式 `--org`，就归入 current org；没有 current org 时归入创建者的个人 org。
- R3：通信判定只有一条：发件人与收件人至少共享一个 org，且该 org 的配置（ACL/链接策略）允许这条通信。主人与 agent 之间也走这条规则；它们天然共享个人 org，所以默认能通，但原因是 org，不是 owner。
- R4：`user:` 收件人同样按 R3 判定。人通过 squire/user-proxy 接收，二者在个人 org 里；发件人必须和收件人有共同 org。
- R5：同一主人的两个 agent 不因为同主人而互通，必须共享 org（Allen 第 7 条）。
- R6：Casdoor group 是成员的唯一事实源。invite/remove 从 org 侧发起，先写 Casdoor，再由成员 actor 落到 OrgFS。节点本地的 OrgFS 成员表只是投影。
- R7：离线设备的重试有预算，预算用完就进入 `dormant`（休眠），不再定时重试。以下明确需求会唤醒一次：向该设备发消息、读取只有它持有的文档、用户执行 `org network retry <device>`、以及该设备的上线 presence。

### 3.3 入站判定伪代码（替换 gate.py 的 bypass 分支和 owner 对免检）

```
admit(sender, recipient):
    if is_internal(sender) or sender == recipient: return ADMIT
    S = orgs_of(sender)            # 来自 OrgFS 目录投影；user 来自 Casdoor 投影
    R = orgs_of(recipient)
    shared = S ∩ R
    if not shared: return REFUSE("NO_SHARED_ORG", orgs=[])
    for org in shared:
        if org.policy.allows(sender, recipient, kind): return ADMIT(via=org)
    return REFUSE("ORG_POLICY_DENIED", orgs=shared)
```

拒绝必须给出原因（`NO_SHARED_ORG` / `ORG_POLICY_DENIED`），并写一条事件 `org.admission.refused`（字段 sender、recipient、sharedOrgs、reason），供 GUI 展示“为什么被拒”。

### 3.4 OrgFS 记录形状（草案）

```jsonc
// <org 目录空间>/agents/<agentId>.json  —— agent 归属（R1）
{ "agent": "agent:owner@example.com:example-node…:hq-adjutant",
  "role": "member", "addedBy": "user:owner@example.com", "addedAt": "…" }

// personal-<userKey> 空间 /receivers.json —— 取代本地 profile 的 preferredReceiver
{ "preferred": { "machine": "example-node…", "machineKey": "…" },
  "squire": "agent:…:squire", "userProxy": "agent:…:user-proxy" }

// <org 目录空间>/devices/<deviceId>.json 增字段 —— R7
{ …, "reachability": { "state": "online|offline|dormant", "failures": 12,
                       "budget": 8, "dormantSince": "…" } }
```

reachability 只在本地观测，不复制到别的节点（每台机器看到的不同）。上面写进 devices 只是为了说明 GUI 读的形状；实现时放在本地状态，由 `org network` 输出。

## 4. CLI 面（与 0.5.5.2 CLI 重组一致）

| 命令 | 作用 | 状态 |
|---|---|---|
| `org list` | 列出本 home 的 org，标出 kind、role、current | 改：加 `kind`、`current` |
| `org use <org>` / `org use --clear` | 设置或清除 current org | 新 |
| `org create / join / leave / delete` | 不变；`delete` 实现为“先 Casdoor 删 group，再标记 OrgFS deleted” | 修 `delete` |
| `org member invite / remove / pending` | 人员成员管理（先写 Casdoor） | 0.5.5.2 已定的重命名 |
| `org member add / remove --agent <agent> [--org <org>]` | 管理 agent 的归属（R1）；与人员成员并在 `org member` 下 | 新（Allen 2026-10-31 定） |
| `org member ls [--org <org>]` | 人和 agent 一起列出，标明类型 | 新 |
| `org pending` | 逐条确认邀请；`--accept-all` 仍保留，但只能显式给出 | 不变 |
| `login` | 结束时列出待处理邀请，TTY 下逐条确认，非 TTY 只报告不加入 | 改 |
| `agent create … [--org <org>]` | 按 R2 选择归属；创建时的快捷写法，等价于随后的 `org member add --agent` | 改 |
| `agent list` | 加一列，显示 agent 所在的 org | 改 |
| `org network [retry <device>]` | 显示设备的 online/offline/dormant；手动唤醒 | 改 |
| `org service` / `org fs` / `agent link` / `user proxy` / `init onboarding` | 0.5.5.2 的搬迁结果，不在本设计内 | — |

新增和改动的命令一律先声明为 daemon request 方法（`MethodSpec`，见 `contract/daemon-request/`），CLI 只做调用和渲染，不在 CLI 里写本地业务；GUI 消费同一组方法。

所有 org 类命令的默认 org 依次取：`--org`、current org，二者都没有时若只有一个 org 就用它，否则报 `ORG_REQUIRED` 并列出候选。

## 5. 删除 owner 机制的清单

| # | 机制 | 位置 | 处理 |
|---|---|---|---|
| D1 | 链接 bypass 默认开 | `composition/links.py:98-156` | 默认改为关；保留开关仅作紧急用途，开启时也走 R3（只放宽链接状态，不放宽 org） |
| D2 | bypass 下“主人属于任一读到的空间”即放行 | `links/service/gate.py:161-165`、`links/store.py:709-722` | 换成 §3.3 的共享 org 判定 |
| D3 | owner 对免链接、不写 OrgFS | `links/model.py:98-105,181,271,286`、`links/policy.py:167,192,253,282` | 删除 `is_owner_pair` 的特权；owner↔agent 由个人 org 的策略放行 |
| D4 | 所有 `user:` 目标免检 | `links/policy.py:160-165` | 改为按 R4 |
| D5 | preferredReceiver/squireChannel 存在本地 profile | `identity/impl/users/profile.py:227-283` | 迁到个人 org 的 `receivers.json`；本地 profile 只读一个版本后删除 |
| D6 | 登录静默接受全部邀请 | `login/flow.py:696-698` | `accept_all=False` 并带 confirm |

需要先确认没有其它地方用 owner 做授权：实现前用 `rg 'owner_of|is_owner_pair|same-owner|bare_owner'` 全量审一遍，结果写进 PR。目前已知的就是上表六处。

## 6. 迁移

迁移做成 **upgrade step**（Allen 2026-10-31：“迁移时agent落入个人org应该做成upgrade脚本，其他节点升级也要用”）：挂进现有机制 `src/hyprial/daemon/impl/upgrade/steps/`，由 `run_upgrade_steps` 在升级后执行，`hyprial doctor --fix` 可重跑待执行的步骤。要求幂等、可在任一节点重复执行，每次输出迁移报告（每个 agent 落到哪个 org、哪些冲突未覆盖）。下面 1–3 都在这个 step 里完成。

1. 升级时为每个已登录用户创建个人 org（`personal-<userKey>`），owner 是本人。设备自动加入，这一步幂等。
2. 现有 agent 的归属：若出现在某个 org-context 的 members/residents 里，就归那些 org；否则归它主人的个人 org。迁移报告列出每个 agent 的结果。
3. squire、user-proxy 移入个人 org；本地 profile 的 preferredReceiver 写进 `receivers.json`，以先升级的那台为准，冲突时报告不覆盖。
4. D1–D4 在同一个版本切换，先以 observe 模式跑一个版本：记录“按新规则会拒绝”的事件但仍然放行，GUI 显示这些事件，然后再切 enforce。
5. 回滚：observe 期间可以直接回滚；切到 enforce 后，回滚只需恢复 bypass 开关。

## 7. GUI 需要的面

实体：org（名称、kind=work/personal、role、current 标记、空间状态）、member（user、role、来源=Casdoor）、agent membership（agent→orgs）、device（机器、在线状态、重试预算）、invite（org、邀请人、时间）。

状态：
- org：`active / deleted / left / sync-problem`
- device：`online / offline / dormant(预算用完)`，附失败次数和最后一次尝试时间
- invite：`pending / accepted / declined / failed(原因码)`
- admission：observe 期的“将被拒绝”和 enforce 期的“已拒绝”

动作：切换 current org；接受或拒绝邀请（逐条）；邀请或移除成员（先写 Casdoor，展示同步进度）；给 agent 加入或移出 org（移出最后一个时禁用并解释原因）；唤醒 dormant 设备；创建或删除 org。

事件（GUI 订阅）：`org.current.changed`、`org.invite.pending`、`org.member.changed`（含 Casdoor 同步结果）、`org.agent.membership.changed`、`org.device.reachability`（online/offline/dormant 转换）、`org.admission.refused`（sender、recipient、sharedOrgs、reason）。

视图建议：
- 顶部 org 切换器，个人 org 固定在第一位，并与工作 org 视觉区分。
- agent 卡片上显示 org 标签。
- 消息被拒时，在会话里显示原因，并给出“去哪个 org 加谁”的操作入口。
- 设备列表区分 offline 和 dormant；dormant 提供“立即重试”。

## 8. Allen 的裁定（2026-10-31）

> 原话：“全部同意，不过，迁移时agent落入个人org应该做成upgrade脚本，其他节点升级也要用。”
> 原话：“hyprial org member add --agent  的命令可以。”

下列 1–6 均按推荐执行；第 7 条来自同次讨论（与 #2077 provider key 归属相关），选 A。

1. 个人 org 能否邀请他人？推荐不能，它只放本人和本人的代理；协作一律用工作 org。
2. 发给 `user:` 的消息（R4）是否也要求共享 org？推荐要求。否则任何人都能直接打扰任何人，与第 7 条的精神不符。
3. agent 归属由谁授权？推荐：加入工作 org 需要该 org 的 admin 或 owner 同意，主人发起申请；加入个人 org 只需主人本人。
4. 迁移是先 observe 一个版本，还是直接 enforce？推荐先 observe 一个版本（0.5.5.3），0.5.5.4 再 enforce，以免上线当天大面积拒信。
5. 重试预算的具体值？推荐连续失败 8 次（约 10 分钟）后进入 dormant，presence 上线或明确需求时唤醒。
6. `org delete` 要不要同时删 Casdoor group？推荐要删：先删 Casdoor（事实源），再把 OrgFS 标为 deleted；成员节点按 leave 处理。
7. agent 使用他人 provider key 的授权记录放在哪里？选 A：放在 key 主人的个人 org。

## 9. 验收（E2E）

- A1：home 加入两个 org，`org use B` 之后，`work`、`org member` 等命令默认作用于 B；`--org A` 能覆盖。
- A2：登录时有 2 条邀请，TTY 下逐条确认；非 TTY 下一条都不加入，只报告。
- A3：同一主人的 agent X（只在 org A）和 Y（只在 org B）互发消息，被拒，原因 `NO_SHARED_ORG`，事件可见；把 Y 加入 A 之后能通。
- A4：agent 与其主人在个人 org 里互通，并且日志显示放行原因是个人 org，而不是 owner。
- A5：移除 agent 的最后一个 org，被拒（`AGENT_LAST_ORG`）。
- A6：设备离线，连续失败达到预算后进入 dormant，此后 10 分钟内没有一次重试；向它发消息会触发一次重试。
- A7：在 Casdoor 里移除一个成员，OrgFS 投影随之更新，该成员的 agent 失去与 org 内其他人的通信。在节点本地直接改 OrgFS 成员表，不会反向写入 Casdoor。
- A8：`org delete` 成功：Casdoor group 被删，OrgFS 标为 deleted，成员节点 `org list` 不再显示它。
- A9：迁移：升级后每个 agent 都有至少一个 org，每个用户都有个人 org，preferredReceiver 来自 OrgFS。
