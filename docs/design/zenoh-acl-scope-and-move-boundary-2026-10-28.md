# Zenoh ACL、OrgFS 与 agent move：职责边界及讨论结论

日期：2026-10-28。来源：Allen 与 `indep-zenohl-acl-implementor` 的直接讨论。
关联：PR #1982，目标版本 0.5.5.2；既有 goal `zenoh-credentials-1982-0552` revision 2。

**结论：zenoh-acl 提供通用身份与资源访问控制机制；move／host 层负责邀请、搬迁授权的业务含义和执行流程。** 本文供 R&D 据此调整派单和验收归属，纠正此前把 move 专用工作直接纳入 zenoh-acl 线的范围混淆。它不是功能实现、运行验收或整个设计包获批的声明。

开发 goal 仍暂停。本次仅按 Allen 要求将讨论结果写入 PR，不恢复产品实施、rebase、测试或部署；原组件成果和未完成验证保留。

## 1. 用户原话和已确定的行为

Allen 对未获授权的搬迁明确要求：

> 应该立刻拒绝

他给出的用户流程是：Alice 先 DM Bob，请求把 agent-1 搬到 host-bob；Bob 执行 `hyprial agent invite <agent-full-address>` 写下授权；Alice 再执行 `hyprial agent move <agent-full-address> to <bob-host-full-address>`，迁移开始前检查授权，没有则立即报错，有则开始迁移。

这些命令是目标交互示意，不是已发布命令的说明。invite 在此表示写入授权，不重新引入此前撤回的邀请链接或 bearer token。

Allen 对主体粒度的原话：

> 邀请具体agent和某个agent，实际上应该只是acl正则匹配的问题？比如allow: alice:* 就是允许所有host上的所有agent，allow:alice:host-a:agent-1 就是具体agent

Allen 最终核对的职责边界：

> 所以move相关的修改不在zenoh-acl的工作范围内，zenoh-acl只提供机制去约束管谁可以写什么类型的东西（比如orgfs的某个space），但具体是什么东西，上层怎么处理，不是zenoh acl负责的范围，对吗？

执行者已确认这一分工，Allen要求落入本 PR。因此，主体模式可以表达某 owner 的全部 agent 或一个具体 agent；先前 owner-only 的推荐不应成为禁止具体 agent 授权的限制。

## 2. 分层与权威

| 层／机制 | 负责什么 |
| --- | --- |
| Tailnet / sidecar | 网络成员身份、固定 authority、lease 与设备连通；不从 OrgFS 邀请反推网络准入 |
| Zenoh 传输 ACL | 根据可信连接身份、操作、网络资源标识，允许或拒绝访问 |
| Hyprial 通用授权机制 | 可信主体模式、资源、动作的匹配，以及可复用的规则表示和判定接口；不硬编码 move 流程 |
| OrgFS | space／文档／blob 的保存、复制和资源映射，包括授权声明的持久化 |
| move／host 业务层 | 管理邀请，解释它是否适用于本次搬迁，在业务执行点调用权限检查并处理结果 |

原生 Zenoh ACL 与 Hyprial 主体授权不能混作一个概念。原生规则用 `subjects`、`rules`、`policies` 等配置表达；连接身份不自动等于同一 daemon 中每个 agent 的完整地址。多个 agent 共用连接时，上层必须提供可信的主体归属，不能仅对请求自报的地址匹配成功就认为身份可信。

OrgFS 提供可信的 space／资源映射，通用机制据此执行访问控制。目前 `orgfs_blob` key 含 digest，并不天然携带完整 space 或 actor 业务上下文；不能仅凭 payload 自报的 `spaceId` 推定授权。文档／manifest 的内容校验及写入后的处理仍归上层。

建立顺序仍为 **tailnet → Zenoh → OrgFS**。OrgFS 中的邀请用于建立通信后的业务检查，不能成为恢复 tailnet 准入的前置。如果上层策略需要转成传输规则，应有明确的装载、生效和撤销机制；写入一份 JSON 不自动改变运行中的权限，也不应形成两份可独立修改的授权真值。

## 3. 主体匹配示意

下面是业务授权的逻辑示意，**不是 Zenoh 原生配置，也不冻结最终 wire schema 或 CLI 语法**：

```json
{
  "resource": "node:host-bob:agents",
  "action": "place",
  "allow": ["alice:host-a:agent-1"]
}
```

- `alice:host-a:agent-1` 匹配这个具体主体。
- `alice:*` 按讨论意图匹配 Alice 名下、来自任意源 host 的 agent，不匹配其他 owner。
- 主体模式无论多宽，都只对本条 `resource`／`action` 授权，不因此取得其他 host 或其他操作的权限。
- “正则／通配”描述匹配能力，不表示已选定任意正则引擎。地址规范化、分段、转义、通配符跨度和完整匹配须在通用契约中定义；不能把 Zenoh key-expression 语法直接当作 agent 地址语法。

资源和动作的业务定义由上层提出，机制负责一致匹配。共享 evaluator 不因动作叫 `place` 就接管搬迁状态机。

## 4. invite / move 的上层目标流程

1. Alice 与 Bob 的 DM 是沟通，Bob 的授权命令产生正式声明。
2. Bob 在 host-bob 对应的 OrgFS 位置保存其控制的授权，绑定主体模式、目标 host 和动作。沿用设备钥签名／验证要求，Alice 不能自行写记录给自己授权。
3. `orgfs/hosts/host-bob/` 是用户给出的逻辑位置示意；实际 spaceId、文档位置和资源映射由 OrgFS／host 层确定，不宣称该路径已经是现有独立 space。
4. move 在停源、传包等搬迁动作开始前验证适用授权；没有有效授权立即拒绝。普通 OrgFS 备份继续遵守其资源权限，不因没有 agent 接纳授权被一概禁止。
5. 目标端在接收搬迁数据、创建／落地 agent 的实际边界调用检查。CLI 提前检查用于早报错，不能代替目标端执行。各处消费同一份适用声明，不各自维护另一套名单。
6. 一次邀请不授予 Alice 对整个 host space 的管理权；查询自己的授权不应泄露该 host 的 agent 清单或所有获准 owner，授权表的修改仍由 Bob 控制。

原 owner 同意本次搬迁、操作幂等、源／目标状态、凭据随迁、回滚和自报均归 move 线。邀请单次／持续有效、通配邀请对未来 agent 的适用性、撤销对在途／已落地 agent 的影响仍需上层设计讨论；不得把未确认的旧推荐写成 Allen 已批准。

## 5. 工作与验收归属

| 工作项 | 归属及处置 |
| --- | --- |
| 设备／连接可信身份、已有凭据存储和客户端、R2 有边界的 authority／adapter／本 home 恢复 | 保留原凭据／通用机制范围；本讨论不删掉已完成组件或未完成 A 验证 |
| 通用主体／资源／动作匹配、规则装载与访问控制执行、公共接口和机制测试 | zenoh-acl／通用授权机制；实际模块接线 owner 由 R&D 明确 |
| `agent invite`、邀请业务 schema／管理、迁移前校验、目标接收与 identity 落地调用 | move／host 层，移出 zenoh-acl 专用实现清单 |
| 拒绝与“目标 actor 不存在”共用的对外 encoder | move 线；机制可以保留内部判定，不将业务错误编码塞进原生 Zenoh ACL |
| bundle 写入、identity 落地各一条真实入口 bytes 对比及无副作用 RED→GREEN | 保留产品要求，移交 move／host 线执行；本线配合机制接口，不以 checker 单测替代 |
| move 幂等、owner 同意、迁移／恢复／撤销后的处置及 E2E | move 线 |
| OrgFS space／资源映射、文档／blob 内容和持久化语义 | OrgFS 层，消费通用访问控制 |

“拒绝与目标不存在逐字节相同”的要求仍保留：两道业务入口各有真实测试，不解析 JSON 后比、不掩码字段、不泄露内部原因。这里调整实现和验收 owner，不删除要求或标记完成。

机制验证与 move 产品验收应分别记录。真实 session／ACL 接线的跨线职责仍由 R&D 与 split E／#1929 明确；本次移交不把缺少的网络执行、旧测试债务或整体验收改成 PASS。

## 6. 请 R&D 据此调整同一套执行文件

- 修订原 GOAL／PLAN／子 goal，明确 B／74 等移交项、接收 owner 和验收条件；移交不等于完成，不另起替代目标丢失原义务。
- 本线若交通用接口，应列出主体、资源、动作、可信输入和机制验收，移除 invite／move 专用控制流程。
- 保留原凭据成果、RED→GREEN、历史证据及未完成验证。本设计记录不证明运行结果，也不要求为纯文档重复全量测试。
- 开发 goal 仍暂停；由 Allen 后续恢复后按调整后的范围执行。本次不改 PR base、不 rebase、不合并或关闭 PR。

## 7. 本次验证与参考

本次仅新增设计文档并给原一页对齐说明增加入口；校验差异、引用和提交范围。产品、测试、contract、CI 配置均不改，pytest／隔离 contract 未运行。本次纯文档提交使用 `[skip ci]` 避免暂停期间因整个 PR 差异触发重型验证；不将 SKIP 当 PASS，后续产品交付仍须完整验证。

原生 ACL 参考 [Zenoh Access Control](https://zenoh.io/docs/manual/access-control/)（2026-10-28 查阅）：官方配置变更需重启实例生效，OrgFS 新增邀请不等于原生 ACL 已自动热更新。本轮不决定新的下发或重启方案。

Contract-Impact: 仅记录讨论，不改变已发布字段、错误码或运行协议；未来机制接口与 move 契约分别在对应实施提交同步。

Skill-Impact: not-needed: 未实现或发布新命令；文中命令为目标交互，操作方式变化时由上层实施提交同步 skill。
