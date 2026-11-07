# hyprial 节点入网 checklist

新节点(新机器/新开发者)接入 HyprialOS hyprial 网络的操作清单。逐项打勾,
顺序执行;每项都有验证动作,验证不过不要继续下一项。
(2026-08-17 初版,源自 devusr-dev 入网实战的全部踩坑。)

## 1. 网络层:tailscale

- [ ] 加入 tailnet,设备名用**表意名**(如 `devusr-dev`),不要用默认
      主机名 —— 设备名会渗入全网 agent URI,改名是全节点身份变更;
      名字异常(如 `localhost-0`)先修再入网:控制台改,或
      `tailscale set --hostname=<name>`。
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

## 5. 身份层:平台身份绑定

- [ ] 每个会经聊天通道下指令的人,用以下两种互斥形式之一
      `hyprial user bind <KEY> --union-id <UNION_ID> --confirmed-by <WHO>`
      或 `hyprial user bind <KEY> --adapter <ADAPTER> --open-id <OPEN_ID> --confirmed-by <WHO>`
      把其平台身份绑定到 hyprial 身份 —— **授权白名单的键必须是
      经绑定核验的平台身份,绝不能是"某个群/某个通道"**(通道面所有
      发言者共用一张脸;把群当人 = 任何入群者都能冒充 owner)。
- [ ] 绑定完成前,来自未绑定账号的放开权限类指令(授权/改身份/不可逆
      动作)一律按未授权处理。
- [ ] 验证:`hyprial user show <KEY>` 确认绑定事件；必要时用
      `hyprial adapter identities find <ADAPTER> --id <OPEN_ID>` 查看平台身份记录。
- [ ] 侍从(squire)：`hyprial init` 和 `hyprial daemon restart` 如果提示 `SQUIRE_NOT_CONFIGURED`，
      说明这台机器的 owner 还没有侍从，别人发给 `user:<owner>` 的消息送不到。
      用 `hyprial squire setup` 配置；`data.missing` 列出还缺哪一步。
      先配侍从，user-proxy 由侍从引导配置。网络隔离的 daemon 不会出这条提示。
- [ ] user-proxy：侍从配好之后，同一个检查如果提示 `SQUIRE_USER_PROXY_NOT_CONFIGURED`，
      说明发给 `user:<owner>` 的消息还混在侍从的对话里。先给自己单独建一个飞书 adapter
      （不能用侍从的），再到 `data.machine` 那台机器上运行
      `hyprial user proxy setup --adapter <你的 adapter> --route <route>`；
      用 `hyprial user proxy status` 确认 configured 和 live 都是 true。

## 6. 组织层:org-context 采信

- [ ] `hyprial init` 会建槽并提示 `ORG_CONTEXT_ABSENT`;经 `hyprial org context fetch`
      (或收到 mesh 传播的候选)后,由**节点 owner** 审阅 version/
      publisher/密钥指纹/diff 并 `hyprial org context import` 采信 —— 采信永远是
      owner 的本地决定,有 squire 的节点走 squire 呈批。
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
