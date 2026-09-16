---
name: repo-workflow
description: 在 harness-bridge/src/gui 中构建、测试、验证与提交 GUI 改动时使用。覆盖静态双端包同步与加载、CI/本地测试、环境变量、入站 ACK、每 Session 独立 actor、身份持久化，以及 GUI 与 Hyprial daemon 的关系。
---

# GUI 子工程工作方式

源码由 dsh-h2b-talk 迁入 harness-bridge/src/gui。下列 GUI 相对路径和 npm
命令均以 src/gui 为工作目录；daemon 代码位于同仓库 src/hyprial。
仓库根 `.forgejo/workflows/gui.yml` 保留四项 GUI 检查；release 包的安装
目录仍为 `~/.hyprial/apps/gui/source`，不带源码仓库的 `src/gui` 前缀。
发布包没有 .git，由 `scripts/gui-source.mjs` 验证来源和文件摘要，不可补一个
假的 Git 仓库或跳过安装检查。合仓实施与真实预演记录见仓库根
`docs/gui-migration/implementation.md`。

本 skill 的每一条都来自仓库内已有证据(主要来源标注在行内),或标注为
hq 协调者提供的一手实测。若与本仓文件冲突,以文件为准并修正本 skill。

## 这个仓是干什么的

依据:`README.md` 首行。

把 DeepSeek Harness(DSH)的 Web UI 改造成「飞书式三人群聊」的 H2B 插件;
同时提供可持久加载的静态 DSH 包(`static/`)和便于快速迭代的动态 Cordis
源码(`imskin-host-plugin.js` / `imskin-plugin.js`)。

## 构建与测试

依据:`package.json` scripts、`README.md`「永久自动加载」「开发验证」章节。

```bash
npm run build:static          # 从 imskin-plugin.js 生成 static/client.js
npm test                      # node --test tests/*.test.mjs
git diff --check
git status --short
```

- 包类型为 ESM(`"type": "module"`),入口 `static/host.js`,peer 依赖
  `@deepseek-ai/cordis ^4.0.1`(`package.json`)。
- 测试覆盖范围见 `README.md`「开发验证」:固定命令与 JSON stdin、daemon IPC
  fence、Actor 派生、精确 allowlist、注入 ledger、多 Session 定向 binding、
  DSH 拒绝时不 mark、Reply/Ack/Send 契约、Cordis timer、静态 Host 路由与
  Client 启动清单契约。

## 门禁

Forgejo GUI CI 位于仓库根 `.forgejo/workflows/gui.yml`,对 `dev/main` 的 push/PR 执行静态
Client 重建、生成物 diff、全套测试和 diff-check。提交前仍须在本地执行上面的
同等命令,并确认生成后没有未理解的改动;不能把远端 CI 当作首次发现明显错误的
调试环境。

真实 H2B 验证按风险执行:修改 bridge、ACK、身份、授权、轮询、收发或静态加载
链路时,按 README 的流程使用专用 Demo Session、单浏览器 Tab 和唯一 marker;
纯文档、样式或隔离测试改动不强制连接真实 daemon。不得把生产或正在工作的
Session 临时接入测试流量。

## 目录约定

依据:`README.md`「目录内容」与仓库结构。

- `imskin-host-plugin.js` / `imskin-plugin.js` —— 动态插件 Host/Client 源码
- `static/host.js` / `static/client.js` —— 自动加载的静态双端包
- `dsh-web.patch.yml` —— 把静态包加入 Web profile 的 patch
- `h2b-session-bridge.mjs` —— H2B daemon 与 DSH Session 的多 Session bridge
- `tests/*.test.mjs` —— node:test 测试
- `scripts/build-static-client.mjs` —— 静态 client 构建
- `docs/` —— B 轨范围与语义、实测证据、团队部署 Prompt
- `${H2B_HOME:-~/.h2b}/state/dsh-web-injected.json` —— Host 写入的注入、参与者、
  飞书 binding 和直聊身份账本。它必须位于升级器会替换的 GUI `source/` checkout
  之外;固定 bridge 调用只把该 state 目录设为 `workspace-write` root,不能继承当前
  Agent Session 的 Workspace,也不能为了规避只读而改到不耐重启的 `/tmp`(见 README)

提交风格依据 `git log`:conventional commits,如 `feat:`、`fix:`、`docs:`。

## 关键约定与坑

### 1. 入站消息必须 ack(重构时不得丢失)

代码佐证:`imskin-plugin.js` 的轮询/注入成功路径调用
`demoRpc('ack', sessionId, { messageId })`;生成的 `static/client.js` 保留相同
逻辑;`h2b-session-bridge.mjs` 的 `ack` 操作转发为 daemon `message.ack`;
`demoAction()` 在 `acknowledged !== true` 时抛错。不要依赖生成文件的固定行号。

不 ack 的后果:h2b 投递层重试到 TTL 后误报 `DELIVERY_EXPIRED`。
**任何重构注入/轮询路径时,必须保留 ack 调用。**

### 2. 每个 DSH Session 派生独立 actor,换 Session = 换收件地址

来源:hq 协调者一手事实(2026-08-18);代码佐证:
`h2b-session-bridge.mjs` 的 `sessionIdentity()` 对 sessionId 做
`sha256` 取前 8 位 hex,actor 为 `agent:<owner>:<nodeId>:dsh-web-<digest>`。

含义:回复只会回到发起时的那个 Session 入口;不能假设换 Session 后旧地址
仍然有效。

### 3. 未经授权的入站身份一律 fail closed

依据:`README.md`。长期身份走 `H2B_DSH_DEMO_ALLOW_FROM` 精确静态白名单;
动态参与者经 H2B `targets` 核验,且只授权给承载该远端 Conversation 的直聊
Carrier(每个 Carrier 最多 3 个动态参与者)。

### 4. 环境变量必须在 DSH 进程启动前设置

依据:`README.md`「最小环境」。三个变量:`H2B_DSH_DEMO_ALLOW_FROM`、
`HARNESS_SOCKET_PATH`(指向 h2b daemon 一侧创建、DSH 进程可访问的 socket)、
`H2B_DSH_DEMO_CWD`(必须是 **DSH 实际运行的机器上**真实存在的目录——
跨机场景把本机路径传过去会出现「worker 在线但 turn 无结果」的难排查故障)。

### 5. 静态 Client 与 Host 必须同步,但加载时机不同

依据:`scripts/build-static-client.mjs`、`package.json`、README「永久自动加载」
以及静态 Host/Client 契约测试。

- `imskin-plugin.js` 是 Client 源码;修改后必须运行 `npm run build:static`,
  `static/client.js` 是生成物,禁止手改。
- 构建脚本只生成 Client。新增或修改 Host RPC 时,必须同时维护
  `imskin-host-plugin.js`、`static/host.js`、两端 operation allowlist 和测试。
- 静态 Client 可在浏览器刷新后重新加载;静态 Host 只有 DSH 进程重启后才会
  重新加载。实现应考虑短暂的 Client/Host 版本错位,尤其不能让归档等基础清理
  因新 Host 操作暂不可用而失效。
- Host 变更后的真实验收必须在受控重启 DSH 后进行;不得为验证擅自中断活跃
  Session。

### 6. 动态 Cordis 插件不持久

依据:`README.md`「动态开发模式」。动态插件不跨 DSH 重启或浏览器刷新;
要持久生效走 `npm run build:static` + `dsh plugin --profile web add` +
`--patch dsh-web.patch.yml` 的静态路径。

### 7. 直聊 Carrier 与工作会话严格 1:1,不同状态有不同持久层

依据:`README.md` 与 bridge ledger 实现。UI 与数据层都拒绝第二个关联。

- 直聊 `DSH Session → canonical Agent`、最近 100 条有界聊天记录和工作会话
  1:1 backlink 写入 package-owned bridge ledger;浏览器缓存丢失后可自动恢复,
  不得从会话标题猜目标。
- 浏览器 `localStorage` 只保留 UI 缓存。用户级离线聚合仍留待正式 carrier/
  server-side persistence。

### 8. 真实 H2B 命令使用完整四段 Agent URI

依据:仓库的 canonical identity/精确授权约束,以及 hq 在 2026-08-18 对当前已发布
H2B 版本的投递实测。人工测试和发单时,`--from` 使用
`agent:<owner>:<node>:<actor>`,不得使用裸 actor 名。当前版本的裸名回复可能投递
到无人接收的键并在 TTL 后过期。H2B PR #168 已修复但尚未发版;正式升级到包含
该修复的版本后,再按新版协议调整这条临时兼容约束。

## 与 h2b 的关系

依据:`README.md` 与 `h2b-session-bridge.mjs`。

本仓是 H2B 的 DSH 侧集成:bridge 经 `HARNESS_SOCKET_PATH` 指向的 Unix
socket 与 h2b daemon 通信(`daemonRequest`,含 `message.ack` 等操作),
把 DSH Session 注册为 h2b actor(见上文第 2 条),实现跨 Agent 的入站注入
与出站回复。h2b 协议与 daemon 本身不在这个仓里。
