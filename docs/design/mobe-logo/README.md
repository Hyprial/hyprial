# mobe 标志设计记录

状态：WIP，标志方向未定。本目录是讨论材料，不是定稿资产；定稿后的源文件归 `Hyprial/design-system`。

本记录只讨论产品命名、标志与头像的视觉方向，不涉及代码、包名、CLI 命令或现有文档里的改名。

## 命名

产品名定为 **mobe**（Allen，2026-11-24）。**m**esh **o**f **be**ings：m-o-be 三段正好对上口号。
公司品牌仍是 Hyprial（拟像未来），mobe 是旗下产品。

### 过程

| 候选 | 结论 | 原因 |
|---|---|---|
| tembox | 放弃 | 最初方案（象 + X，tem + box，致敬 Chatbox）。两轮标志都不满意后，改从 “mesh of being” 出发重新命名 |
| mob | 放弃 | PyPI、npm、crates.io 均被占；`mob` 命令与 mob.sh（mob programming 的 git 交接工具）冲突 |
| mobs | 放弃 | PyPI 被占；与 `mob` 只差一个字母 |
| mobby | 放弃 | 包名三处都空，但 `.com / .ai / .dev / .io` 都被占 |
| mobit | 放弃 | npm 被占（弃置包）；`.com / .io` 被占；同名公司多 |
| mobkit | 放弃 | npm 被活跃项目占用；`-kit` 读起来像工具包 |
| mobly | 放弃 | PyPI 上是 Google 的 Mobly 测试框架，活跃，月下载约 23 万 |
| **mobe** | **采用** | PyPI、crates.io 空；npm 是 2016 年的弃置包 |

### mobe 的占用情况（2026-11-24 实测）

| 项目 | 结果 |
|---|---|
| PyPI `mobe` | 空 |
| crates.io `mobe` | 空 |
| npm `mobe` | 被占：单个 0.0.1（2016），无描述，上月下载 20 次 |
| `mobe.com`、`mobe.ai`、`mobe.dev` | 已注册 |
| `mobe.io` | 已注册，停放页 |
| `mobe.sh` | 查不到 NS 记录，可能未注册 |

方法：包仓库查 JSON API 的 HTTP 状态；域名用 DNS-over-HTTPS 查 NS 记录。已注册但未配置解析的域名会被误判为空。

### 未验证与已知风险

- 商标检索：UNKNOWN。
- GitHub 组织名 / 用户名：UNKNOWN（会话无权限查询）。
- 英文搜索 “MOBE” 可能出现 2018 年被 FTC 关停的 My Online Business Education 相关结果；另有一家同名的美国健康服务公司。
  这两条来自 Claude 的记忆，本次检索未复现，采用前需人工复核。
- 读法未定（mob / moh-bee / 与 robe 押韵）。
- npm 包名拿不到，JS 侧需用 scope 或别名。
- PyPI 与 crates.io 的 `mobe` 尚未占位，需持发布账号的人处理。

## 标志

### 已确认的输入

| 问题 | 结论 |
|---|---|
| 品牌关系 | 公司 Hyprial，产品 mobe，沿用 design-system 色板 |
| 风格 | 起点是“像素小方块”，后改为参照 Grok、OpenAI 这类 personal agent 的抽象风格 |
| 判断标准 | 辨识度和记忆点优先 |
| 使用场景 | 终端 / CLI、favicon / GUI 图标、Lark 与聊天头像、README；一个主标志，各平台可有变体 |
| 头像 | 标志风格决定 app 内头像风格；现有代码符号脸与 app 不协调，一并重做 |

色板取自 design-system v5：纸白 `#FAFBF6`、墨黑 `#17131F`、紫蓝 `#7192D8` / `#C9D2E7`、
青柠 `#D7F279` / `#DFF493`、深绿 `#14804A`。

### 前两轮（均已作废，材料见本分支 git 历史）

- **第一轮（tembox）**：四版像素方块小象。反馈：都不满意。
- **第二轮（tembox）**：参照 personal agent 标志，改为“一个盒子加一个动作”，三个方向（尾鼻、括号箱、九点箱）。
  未及定论，产品名改为 mobe，盒子与象的前提不再成立。第二轮文件在提交 `be755e7e` 的 `docs/design/tembox-logo/`。

第二轮总结的五条做法继续沿用：一个基本形；只动一刀；单色先成立；标志就是状态；能被打出来。

### 第三轮（mobe）

基本形换成点：being 是一个点，mesh 是点之间的关系；全图只亮一个点，它同时是状态。大象、盒子、信箱不再出现。

| 方向 | 构成 | 记忆点 | 风险 |
|---|---|---|---|
| A 三人 m | 三个点，下接一道横杠和三条腿，整体是小写 m | 手拉手的三个人就是 m；亮点在三人之间传递 | 远看像皇冠、城垛或“山” |
| B 盲文 m | 三个点，按盲文 m（1、3、4 号点）排布 | 能直接打出 `⠍`，名字可写成 `⠍⠕⠃⠑` | 小尺寸接近通用的“更多”图标；点位不能为好看挪动 |
| C 圆桌 | 八个点围成一圈，一个点站在圈外半步 | 有一个点不在队里，归队、离队、绕圈即全部状态 | 静态时最像加载动画 |

当前推荐 A（Claude 的建议，未经 Allen 确认）。

### 头像

提案：每个头像是一块实色底加三乘三点阵，点阵由名字哈希得出；**人的底是圆的，agent 的底是方的**。取代代码符号脸。

## 待决

1. 三个方向选哪一个继续。
2. mobe 的读法。
3. 头像规则是否采用。
4. Hyprial 的盒子是否要在 mobe 标志里留痕迹（第三轮按“不留”做）。
5. 定稿后 design-system 需要改的条款：`DESIGN.md` 现写“HyprialOS 没有单独的产品标志”“不把标志用作头像”。

## 文件

- `round-3.html`：第三轮完整探索页，浏览器直接打开。
- `svg/mobe-r3-{trio,braille,ring}-{light,dark}.svg`：三个方向待命态的讨论用矢量稿。

## 未实测

- 真实 GUI、Lark 头像裁切、真实终端下的效果：UNKNOWN，页面中的用法都是模拟。
- 盲文字符 `⠍⠕⠃⠑` 在各终端字体下的显示：UNKNOWN。

## 参考

- Grok 标志沿革：<https://designcompass.org/en/2025/02/25/grok-new-logo>、<https://1000logos.net/grok-logo/amp/>
- ChatGPT 界面的点与语音球：<https://techcrunch.com/2024/05/13/chatgpts-new-face-is-a-black-hole>
- mob.sh：<https://github.com/remotemobprogramming/mob>
- Google Mobly：<https://github.com/google/mobly>
