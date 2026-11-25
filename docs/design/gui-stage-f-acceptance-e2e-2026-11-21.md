# Stage F 验收作为 E2E 场景（占位，未开始）

状态：**未开始实现；逐行核对已完成（2026-11-21）**。owner 2026-11-21：Stage F 验收建 WIP PR，按 E2E 场景来做。
本文只记录已定的方向、范围与开工前待定的问题，使这件事不只存在于对话里；不是实现计划，不授权任何代码改动。

## 背景

GUI 启动模式统一的实施线已结束（单一启动路径、daemon-first、单一宿主进程；macOS 实机验收见 #2980 评论）。
剩下的验收项是 Stage F：`docs/design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md`
列出的八类组织页场景。它们原由两个浏览器脚本 `org-lifecycle` 与 `org-rework` 断言，
脚本在 1e-4 删除（KP-063），至今没有别的浏览器脚本覆盖。

## 已定方向

- 验收以 E2E 场景的形式落在 `contract/e2e/`：场景文档、runner、`scenarios.json` 里的条目与 `covers`，
  与现有场景相同的三态纪律（PASS / FAIL / BLOCKED，skip 不是结论）。
- 被测对象是产品真实入口：隔离 home 上的真实 daemon、`hyprial gui` 启动的宿主进程、真实浏览器。
  不读取或改动生产 daemon、凭据与用户级配置。
- 八行的断言文字以 `1e-4-stage-f-acceptance.md` 的表为准，本文不重述。

## 逐行核对结果（2026-11-21，对照 dev `ce0ba7d70`，只读代码，未运行）

本节取代原先的“范围”表。归类：REAL＝隔离 home 上的真实 daemon 能到达；MOCK＝真实 daemon 不产生该状态，只能由
Contract Mock 驱动的浏览器脚本承担；DROP＝该行的对象即将删除。

| 行 | 旧编号 | 界面是否仍在 dev | 归类 | 今天能否做 | 受在途工作影响 |
|---|---|---|---|---|---|
| 1 | L1+L2 | 在（`InvitesSection.tsx` 的 `JoinByLink`） | REAL | 不能直接做：隔离 daemon 不能加入组织，需要 E2E-025 那样的两个 daemon 加 tailcat 旁路；pending 窗口在真实 daemon 上不可控 | 会变：成员改由 org owner 的机器管理并以 uid 为键、取消个人组织（合并线计划 OC3、OC4） |
| 2 | L3 | 在 | MOCK | 不能：`steps[].kind` 是契约枚举，非法值在宿主处就变成 502 `INVALID_RESPONSE`，浏览器看不到；Mock 的示例也必须符合结果 schema，所以连声明都做不到。可达的替代是一个 `dispatch: unknown` 的 `org.join` 示例，目前没有 | 会变（OC3） |
| 3 | F2.1 | 在，但顶层“机器”页已于 2026-11-16 删除，机器列表在“成员 → 设备”里，刷新按钮在成员区标题上 | MOCK | 不能：Mock 的状态转移只在 200 应答后触发，表达不了“先失败后成功” | 无 |
| 4 | F3 | 在（位置同上） | REAL | 差两样：能进入组织页的就绪 home，和浏览器驱动。两个组织用两次本地 `org create` 得到，不是“加入两个组织” | 暂无 |
| 5 | F3.1 | 在（位置同上） | MOCK | 不能：没有“扣住一个应答、稍后放行”的手段 | 无 |
| 6 | F1 / F1b | 在（`RepairDialog.tsx`） | MOCK | 不能：缺三个具名示例（全部只能手动的预览、`published: false` 的执行结果、`partial` 的组织） | 无 |
| 7 | F4 | 在（`AgentOrgPanel.tsx` 的“通信”） | DROP | 不做 | 将删除：发送许可整体删除（owner 2026-11-19）。截至本日 `link.*` 仍在 dev，删除 GUI 调用点的切片尚未合入；本行随该删除一并移除 |
| 8 | F5 | 在；`org-write-unavailable` 提示在 `gui/src` 里已不存在 | MOCK | 不能：Mock 把所有方法都声明为可调用，需要目录变体 | 无 |

对所有行都成立的事实：

- 八行现在都只有组件或 store 级的测试（`gui/tests/modules/dev04/`），没有任何一行在浏览器里被断言。第 1 行的同步两次
  `requestSubmit()` 连组件级覆盖也没有。
- 真实 daemon 的 GUI 今天进不了组织页：`AppShell.tsx` 在账号已认证、设备 `ready`、首次运行已记录或已跳过之前都转到首次运行页，
  而 e2e 机制只设置 owner。补这个缺口的是 #3048（`tests/support/gui_home/`，尚未合入）。
- `contract/e2e/` 里没有任何场景驱动浏览器或启动 `hyprial gui`；Python 依赖里没有浏览器。唯一把真实 daemon、`hyprial gui`
  和 Chromium 接起来的是 `gui/tests/daemon-request-host/browser-acceptance.mjs`，它不在任何 workflow 里，且 16 个用例有 10 个是红的
  （KP-076，owner 裁定不修）。
- 现在没有任何场景的 `covers` 包含 `gui/**`。让新场景覆盖 `gui/src/features/org/**`，会给之后每个组织页 PR 增加一行
  `E2E-Impact` 声明。
- 下一个空场景号是 E2E-032。
- e2e 里创建或加入组织不联系公网身份发行方（owner 来自环境变量或设备密钥）；`hyprial login` 会联系，不能用。OC3 之后加入要向
  owner 的机器出示登录 token，届时加入路径会需要发行方或 token 夹具（推断，依据计划）。
- 5 行 MOCK 没有一行是 Contract Mock 今天能产生的。需要新增的能力：在错误应答后或第 N 次调用后触发的状态转移（第 3 行）、
  可控放行的应答（第 5 行）、目录变体（第 8 行）、若干具名示例（第 2、6 行）。`1e.md` 3.5 节只列了预设、具名示例和“是否要状态转移”，
  没有列应答时序和目录变体。

读代码时看到、没有运行验证的一个可能缺陷：隔离 daemon 上 `org.repair` 的执行结果里 `published` 为 `null`，而结果 schema 声明它是布尔值，
宿主会答 502。

## owner 裁定（2026-11-21）

- 同意本文方向；实施交给 Mac 打包线的开发者。
- 真实 daemon 不会产生的状态留在 Contract Mock 驱动的浏览器脚本里；E2E 场景只承担真实 daemon 能到达的那几行。
  每一行归哪一边，由实施者在当前 dev 上逐行核对后列表给出；归 Mock 一边的行仍按 `1e-4-stage-f-acceptance.md`
  的验收条件（只经 daemon-request 路径取数的浏览器脚本）。

## 仍待 owner 裁定

- 场景编号（开工时取 `scenarios.json` 的下一个空号）与是否计入发版门。
- 是否把 `browser-acceptance.mjs` 里那种测试自带的 socket 丢包代理算作“真实 daemon”的手段（算的话第 3 行可归 REAL）。
- 实施时机：第 1、2 行随 OC3、OC4 会变，是否等组织页改造落定后再做。
- Contract Mock 为归它的那几行需要新增的状态、次序或目录变体（`1e.md` 3.5 节的开放问题），属于 Mock 来源的改动，须先列出再做。

## 不在本文范围

Stage F 的另一半，即用 Contract Mock 替换手写 fixture 并清空 `scripts/ci/gui-mock-source-ledger.json`，
见 `1e.md` 3.5 节与第 5 节；本文只管验收场景。
