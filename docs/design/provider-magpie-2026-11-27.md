# Provider 登录与密钥管理交给 Magpie（WIP，访谈中）

状态：只有方向和待定问题，没有实现，不改变现行契约。设计由负责人逐问裁决，全部确认之前不写实现。
本线在「与其他线的关系」一节列出的 PR 合入之前不开工。

## 1. 负责人已定的方向

2026-11-26：

1. 模型密钥交给 Magpie 和 `hyprial provider` 处理。
2. 交互终端里不出现密钥，由 Magpie 管理和投影。终端里只有网关地址和模型 ID。
3. `hyprial agent provider login` 全部改为 `hyprial provider login`，登录通过调用 Magpie 完成。
4. hyprial 共用用户自己的 Magpie 实例，不打包进 wheel。daemon 复用已在运行的网关，没有运行时再启动它。

2026-11-27：

5. hyprial 后端不再承担 provider 的管理职能，直接依赖 Magpie。daemon 仍提供 provider login 的方法，CLI 和 GUI 仍提供入口，后端调用的是 Magpie 的能力。
6. 各 harness 的原生订阅登录（OAuth 类）也经 Magpie。`hyprial provider login <harness>` 由 daemon 调用 Magpie 的登录流程，harness 一律指向网关。hyprial 不再按 agent 保存原生登录文件，`agent.provider.login`、`agent.provider.logout`、`agent.provider.default` 这组方法移除。

7. hyprial 不保存任何模型 Key。无头启动与交互启动拿到的都只是网关地址和模型 ID。用户添加的模型连接条目、owner-key 和机器默认凭据里的模型 Key 不再保留；保存和测试模型连接的 daemon 方法 `agent.secret.provider-configure`、`agent.secret.provider-test` 移除；启动时不再向 CLI 进程或子进程下发 Key。Magpie 因此成为必需组件：没有安装或网关不可用时，agent 无法调用模型。已保存的 Key 由用户按迁移文档重新录入 Magpie。这一条只针对模型 Key：非模型服务的 Key 怎样存放由服务授权线决定，不在本线范围。
8. agent 的进程只拿占位符，不拿任何网关 key。本机所有 agent 共用 Magpie 里的全部账号和模型。绑定里的模型 ID 是 hyprial 一侧的约定，网关不强制；额度和限流按 Magpie 的全局设置。按 agent 限制账号和模型不在本线范围内。
9. 服务授权交付的模型网关 Key 写进接收方的 Magpie，hyprial 不留副本。接收方的 daemon 在本机 Magpie 里登记一个自定义 provider，地址是本机映射端口，Key 是授权来的那把；授权被撤销或过期时由 daemon 从 Magpie 删除。接收方不再需要把 Key 绑给 agent，也不需要手工建模型连接。授权方一侧由 daemon 向自己的 Magpie 申请一把网关 key 交出，可以限定模型、账号和额度，不从 hyprial 的条目里取。第 9 条由本线实现，只改「服务是模型网关」这一支。
10. 模型连接这串 PR 的后续开发由本线接手。原作者已于 2026-11-27 交接，交接说明在 PR #71 的正文和模型连接设计 §7.2、§7.3；九个 PR 的头提交与交接表一致，之后没有新提交。PR 保持打开，由本线继续开发。接手范围是 PR #59、#61、#81、#82、#83、#96、#84、#85、#86。其中 #59、#61、#81、#82、#83、#96 与本线的方向不冲突，本线不改它们的内容，只负责让它们通过检查，由负责人按原顺序合入。#84、#85、#86 保持打开不合入，本线设计定稿后在它们的分支上改成 Magpie 方案，或另开 PR 取代后关闭。PR #71 的收口也由本线承担。
11. daemon 持有一把只限本机的 Magpie 管理 key，经 Magpie 的管理接口完成登录、录入 Key 以及第 9 条的登记和删除。这把 key 由 daemon 在首次需要时通过命令行在用户的 Magpie 里建立并设为本机管理，存在 hyprial 的私有目录，不进入 agent 的进程和终端。它不是模型 Key，第 7 条不受影响。模型 Key 不经过命令行参数。
12. 一条绑定里的模型可以是本机 Magpie 网关列出的任何 ID，包括 `group/<档位>` 这样的分组。可选的 ID 来自网关，不在 hyprial 里写死。`hyprial agent model configure` 的 `--connection` 和 `--config-revision` 删除，绑定里不再有指向连接的字段。daemon 在配置时向网关核对这个 ID 是否存在。恢复会话时的模型一致性检查只比较绑定的 ID。
13. `hyprial provider` 是一族顶层命令：`login <名字>`、`logout <名字>`、`list`。订阅登录和按 Key 接入的厂商都走它。可用的名字由 daemon 向 Magpie 查询，不在 hyprial 里写死。订阅类由 daemon 调用 Magpie 的登录流程，CLI 只显示链接和用户码并等待。Key 类由 CLI 用不回显的提示或标准输入读入 Key，交给 daemon 写进 Magpie。GUI 使用同一组 daemon 方法。命令行上的 `hyprial agent provider` 和 `hyprial user provider` 两族不再保留；按 agent 授权 Key 和轮换 Key 的功能随第 7、8 条取消。
14. 名词用 provider，账号是它下面的一层，与 Magpie 的用词一致。一个 provider 下可以有多个账号或多把 Key：`hyprial provider login <provider>` 每执行一次就多加一个；`list` 按 provider 列出它下面的各个账号和 Key，不显示 Key 的值；`logout <provider> [账号]` 可以只退出其中一个。daemon 一侧改写现有的 `user.provider.login`、`user.provider.logout`、`user.provider.list`，方法名不变，后面调用 Magpie，并对 GUI 开放；`user.provider.rotate` 和 hyprial 自己的 Key 封存存储删除。这组方法的契约随之修改。
15. PR #84、#85、#86 不在原分支上改，第 10 条留下的选择定为另开 PR 取代。前六个 PR 合入后，本线从 `dev` 另开新 PR，按 daemon、CLI、GUI 的顺序切片，三个旧 PR 里能用的部分摘过来。新 PR 合入后关闭这三个旧 PR，并在 PR #71 里记下取代它们的提交。

这些方向与模型连接设计 §6（暂不把 Magpie 作为必需组件）和 daemon 级 provider 登录设计（不新增顶层 `hyprial provider` 命令族）的结论不同，是在它们之后做出的决定。

## 2. 尚未决定

- 第 14 条有一点尚未核实：Magpie 的管理接口能否删除单个订阅账号（它的命令行帮助里没有这样的命令）。实现前先确认。
- 第 15 条有一点尚未核实：三个旧 PR 里各有多少能直接摘用，还没有逐个核对。
- 第 12 条有一点尚未核实：分组在会话中途换了实际提供服务的模型之后，恢复检查和 thinking 签名是否受影响。实现前先测。
- 第 9 条有一点尚未核实：授权方的网关是否校验交出的那把 key（流量经转发后从授权方本机回环进入网关时）。
- 第 11 条有两点尚未核实：第一把管理 key 是否只能通过命令行建立；管理接口里的登录流程能否由 daemon 转给 GUI。
- daemon 级 provider 登录设计（PR #68）的处置。
- Windows 的范围。

## 3. 与其他线的关系

- PR #59、#61、#81、#82、#83、#96：与本线的方向不冲突，照原顺序先合入。本线负责让它们通过检查，不改内容；本线的实现基于它们合入后的代码。合入用 merge commit：按顺序 squash 时从 #81 起会与 `dev` 冲突。每合一个，下一个要手动改基到 `dev`。
- PR #83 与 #96：#83 的头单独过不了 gate，有 20 条契约和 CLI 清单的失败，由 #96 修掉，所以 #96 必须紧接 #83 合入。#96 的头另有 1 条 provider 术语检查的失败，来自它新增的一行帮助文本。负责人 2026-11-27 裁决：#83 由负责人以管理员权限合入，紧接着合入 #96；#96 那一行帮助文本由本线按 #84 里的写法改，这是「不改内容」的唯一例外。
- 必需检查 gate 还不在 `dev` 上，它在 PR #78 里。#78 合入之前这六个 PR 都得不到 gate 的结果。
- 交接说明把 #84、#85、#86 排在 #96 之后依次合入。按第 6、7 条它们不按原样合入，属于交接说明里的「替代或关闭处置」，届时记录等价实现的提交。
- 交接说明指出的 9 项原生登录用例与全量测试所用 Pi 版本不匹配，位于 #84。本线在取代 #84 时一并处理，不记为迁移遗留。
- PR #71（模型连接实施记录）：按交接说明保持草稿，九个 PR 全部合入或有明确处置后由本线收口，记录实际的合并提交和处置链接。
- PR #84（原生登录的精确绑定）：它扩展的部分会被第 6 条移除，已请该 PR 等本线的实现，不先合入。
- PR #85、#86：叠在 #84 之上，与 #84 一起等本线的实现，不先合入（负责人 2026-11-27 裁决）。#85 的连接页把地址和 Key 存进 hyprial 自己的存储，与第 7 条不符。
- #96 的帮助文本改动推送后（头 `d5aae74ac6`），#84 的分支与 #96 在两个文件上冲突。按第 15 条 #84 不合入，这个冲突不需要解。
- 服务授权线：按现计划合入，本线等它合入。第 9 条在它合入之后由本线实现，已告知该线不需要为此改动。
- 交互终端宿主线：不等待。两边只在启动参数处交汇。

## 4. 约束

- 业务逻辑在 daemon，CLI 和 GUI 只请求和渲染。
- 不写迁移代码，迁移以文档形式给出。
- 能做成运行时数据的不写死。
