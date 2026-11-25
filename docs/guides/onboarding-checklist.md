# hyprial 节点入网 checklist

新节点(新机器/新开发者)接入 HyprialOS hyprial 网络的操作清单。逐项打勾,
顺序执行;每项都有验证动作,验证不过不要继续下一项。
(2026-08-17 初版,源自 devusr-dev 入网实战的全部踩坑。)

## 0. 快速上手:第一个 agent 与飞书接入

首次运行(`hyprial init`、`hyprial login`)只创建 home、启动 daemon 并登录本机用户,
**不会创建任何 agent,也不会配置飞书**。下面每一步都由显式命令发起;没有一条命令会
替你决定 agent 名、adapter、route 或 pin 目标。

1. 初始化并登录(见[运维指南](operator-guide.md)):

   ```bash
   hyprial init
   hyprial login
   ```

2. 选择名字,创建第一个 agent。`--provider`、`--model`、`--preferred-harness` 可省略,
   省略时才使用该 agent 的默认值;名字在本机唯一,只有 `hyprial agent destroy` 能释放。

   ```bash
   hyprial agent create --name <name> --provider <provider> --model <model> --preferred-harness <harness>
   ```

   `--json` 输出里的 `agent.uri` 是权威地址 `agent:<owner>:<machine>:<actor>`,第 7 步要用。

3. 用你选定的 harness、cwd、provider/model 与 Pi approve 设置启动它:

   ```bash
   hyprial agent start <harness> --name <name> --headless
   ```

4. 接入飞书 App。已有 App 用 `--app-id` 与 `--secret-file` 注册;没有 App 时
   `adapter add` 走官方的人工创建流程:

   ```bash
   hyprial adapter add <adapter> [--app-id <id> --secret-file <path>]
   ```

5. 授权租户 scope,然后检查:

   ```bash
   hyprial adapter scopes authorize <adapter> --interactive
   hyprial adapter scopes check <adapter>
   ```

6. 让机器人收到一条真实消息,用 `adapter route candidates` 观察入站 chat;native id 必须由
   你确认,然后添加出站 route:

   ```bash
   hyprial adapter route candidates <adapter>
   hyprial adapter route add <adapter> <route>=<native-chat-id> --default
   ```

7. 把 adapter 绑定到这个 agent(agent 必须已存在):在它的 cell 文档里加一个 adapter 成员。
   先读出 cell 与 revision,在 `members` 加 `{"ref": "<adapter>", "role": "adapter"}`,在
   `params."cell.members"."<adapter>"` 写 `{"adapter": "<adapter>"}`,再按 revision 写回。一个 agent
   可以绑定多个 adapter;同一个 adapter 只能出现在一个 agent 的 cell 里,否则入站路由失败关闭:

   ```bash
   hyprial agent cell show <agent> --json
   hyprial agent cell apply <agent> --expected-revision <N> --file CELL.json
   ```

8. 用 `hyprial message send` 与真实飞书消息验证入站、回复与重启后的行为:

   ```bash
   hyprial message send --from <agent-uri> --to <route-or-user> --summary <nonce> "<text>"
   ```

需要人工、脚本无法代替的步骤:

- 真实飞书 App 的创建与租户 scope 授权仍需在平台上由人确认;未完成时按 `BLOCKED` 记录,
  不要用 mock 结果代替真实往返。
- `adapter route candidates` 只能列出已观察到的 chat;选择哪个 native id 由你决定。
- 没有一条通用命令会把上面五步打包成无人值守动作;需要自动化时,由调用方按上面的通用命令
  自行编排,不要恢复首启编排。

## 1. 网络层:tailscale

- [ ] 加入 tailnet,设备名用**表意名**(如 `devusr-dev`),不要用默认
      主机名;名字异常(如 `localhost-0`)先修再入网:控制台改,或
      `tailscale set --hostname=<name>`。
      (2026-11-20 起 node id 不再取主机名:新 home 由 `hyprial init` 写入
      随机的 `<home>/node-id`,改主机名不再改变 agent URI;已有安装见
      `docs/guides/migration-im-push-by-agent-binding.md` 第 13 节。)
- [ ] 验证:`tailscale status` 双向可见、`ping` hq 节点通。

## 2. 传输层:zenoh 对等连接

- [ ] **不需要填任何人的地址。** daemon 启动时会问本机 tailscale 要
      tailnet 内的 peer 表,自己去试连 —— 你只要在这个 tailnet 里。
  - 这一格从前写的是「配 `connect` 指向 `tcp/100.64.30.3:7447`」。
    那是一个**写在文档里的常量**:hq 换地址就得每台机器手工改,而
    **谁改了谁没改没有任何东西知道**;对仓库之外的使用者它更是毫无意义。
    ⚠️ 而它还有一个不明显的后果 —— 成员之间**没有任何机制互相发现**,
    所以人人看得见 hq、彼此互不可见(2026-08-28 的 P0 就是这个)。
- [ ] **`listen` 也不用配了** —— daemon 会自动用【本机的 tailnet 地址】
      加约定端口 `7447` 监听,别人因此连得到你。
  - 也就是说这一节现在**一条都不用填**:装好 tailscale、`hyprial init`,就完了；
    首次 init 没有 owner 时会自动进入 `hyprial login`,不需要预先设置身份。
  - ⚠️ 自动推导**永远用 tailnet 地址,绝不用 `0.0.0.0`** —— 整套安全性
    建立在"只有 tailnet 内进得来"上,而监听所有网卡会当场废掉这个前提。
  - 要手工指定时,显式 `--listen` / `HYPRIAL_ZENOH_LISTEN` **压过自动推导**。
  - ⚠️ 若 daemon 推导不出地址(没装 tailscale 等),它会**明确警告
    「别人将无法看见你」** —— 这条警告存在的理由:只连出去的节点
    **从自己这边看一切正常**(它看得见所有它拨过的人),所以
    "没人看得见我"是它自己永远发现不了的。
- [ ] tailscale ACL 放行 7447。**可见性由 ACL 决定**:发现只会拿到
      ACL 允许你看到的 peer,所以隔离靠 ACL 配,不靠少填地址。
- [ ] 需要关掉自动发现时(排查/隔离):`HYPRIAL_PEER_DISCOVERY=0`。
      ⚠️ 拼错会**报错**而不是静默当成关 —— 免得你以为关了其实没关。
- [ ] 验证(三条都要,缺一不可):
  1. 本方 `hyprial message targets` 出现 hq 侧 agent;
  2. **hq 侧 `hyprial message targets` 也出现本方 agent**(单向可见≠连通:mesh
     中继会制造"通着"的假象);
  2b. **另一个成员的 `hyprial message targets` 里也出现你**,而不只是 hq ——
     这一条是 2026-08-28 之后新增的:只验"我和 hq 互见"会漏掉
     "成员之间互不可见"那整类故障,而那正是当时没被发现的原因。
  3. 对端 `ss/netstat` 的 ESTABLISHED **不可信** —— hq daemon 重启后
     旧连接在你方内核里永远显示 ESTAB(半开僵尸)。以 **listener 侧
     lsof** 和双向 targets 互见为准。hq 重启通告后,重启本方 daemon
     重拨(自动保活/重连修复落地后此项作废)。

## 3. 寻址

- [ ] 跨机直发用**四段 URI**:`agent:<owner>:<node>:<actor>`;裸名
      (`agent:<actor>`)目前只在同节点内解析(逻辑寻址落地后放开)。
- [ ] 验证:向 hq-adjutant 四段直发一条,收到回执终态为
      `fetched` / `ACK_RECEIVED`（未收到 ACK 而到期则为
      `expired` / `TTL_EXPIRED`）。

## 4. 版本

- [ ] `hyprial daemon upgrade` 到最新 tag(升级自动重启 daemon)。
- [ ] 验证:版本号与发版通告一致。

## 5. 身份层:人员登记

- [ ] hyprial 不把人和 IM 账号绑在一起:经 adapter 来的消息,说话人就是拥有该平台
      账号的人,hyprial 不判定他是不是 owner;owner 的指令以 `user:<owner>` 的身份
      到达(GUI、CLI)。需要时用 `hyprial user add` 登记人员(只有名字)。
- [ ] 放开权限类指令(授权/改身份/不可逆动作)只认 `user:<owner>`;经聊天通道来的
      一律不当作 owner 的授权。
- [ ] 验证:`hyprial user show <KEY>` 显示登记的人;需要时用
      `hyprial adapter identities find <ADAPTER> --id <OPEN_ID>` 查看平台身份记录。
- [ ] 飞书副本(可选):发给 `user:<owner>` 的消息默认只留在 GUI 与 CLI，不会同步到飞书。
      需要飞书副本时，给这个 owner 单独建一个飞书 adapter(不要复用其他用途的 adapter)，
      把本机用户 agent(`hyprial-user`)的 cell 绑定到它和目标聊天，见
      `docs/guides/migration-im-push-by-agent-binding.md` 第 4 节；
      用 `hyprial agent cell show hyprial-user` 确认绑定。

## 6. 组织层:org-context 采信

- [ ] `hyprial init` 会建槽并提示 `ORG_CONTEXT_ABSENT`;经 `hyprial org context fetch`
      (或收到 mesh 传播的候选)后,由**节点 owner** 审阅 version/
      publisher/密钥指纹/diff 并 `hyprial org context import` 采信 —— 采信永远是
      owner 的本地决定。
- [ ] 采信的名册即身份层(第 5 项)的校验源:绑定谁、信谁的签发,
      对照名册核。
- [ ] 验证:`hyprial org context show` 显示已采信版本。
- [ ] 手改正文前后先跑预检:`hyprial org context validate`。默认槽同时校验两步:
      正文格式(围栏 YAML 的 line/column/reason),以及已采纳槽一致性 ——
      `org/adoptions/<正文原始字节 sha256>.json` 必须存在、是合法 JSON,且
      version/publisher/documentSha256 与正文一致、adoptedAt 合法;存在
      `org/publish-pending.json` 时还须指向当前正文。任一步不一致都 `ok:false`、退出 1,
      修正后再采信。`--path <file>` 只做格式预检,不查采纳状态。
- [ ] 采纳状态语义:`org-context.md` 正文只是可解析的视图;"已采纳" 由同摘要的
      adoption 记录证明。记录缺失或不可读时,`org show`/`org status` 仍展示正文,但省略
      `adoptedAt`,并在 stderr(`--json` 时在结果 `warnings` 里)明说缺采纳记录 ——
      不要把它当成已采信,也不要手工补一个记录。

## 7. 通道纪律(有 lark adapter 的节点)

- [ ] worker/agent **禁止**发起 `auth login/logout` 设备流(会顶掉既有
      用户 token),只允许 `auth status`;登录流一律由人/协调者操作。

## 8. 完成通告

- [ ] 在全员群通告:节点名、owner、承载的 agent 清单(供各节点
      org-context 名册更新参考)。

---
维护:hq-adjutant。协议或流程变更时同步更新本单;每次新节点入网踩到
新坑,坑进这里。
