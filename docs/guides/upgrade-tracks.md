# hyprial 三通道升级轨道

发布规则唯一事实源：[发布规则](release-rules.md)（Allen 2026-10-28，0.5.5.2 起执行）。
本文保留客户端行为与历史；历史裁定不覆盖最新发布规则。

## 历史：2026-10-26 发版裁定（当时取代手工 stable / main-pointer 规则）

Allen 原话：

> 发版逻辑有问题，我们此前规定是这样的，每当版本号有变动，比如说此次从 0.5.5 发展到 0.5.5.1，那么这个版本号变动是要推送到 Stable 这个 track 的。
> Internal track 永远都不变动版本号，它仅仅是跟随这个 Dev 的 Head ，dev 上最新是什么版本，它就是什么版本。
> 然后 Nightly 理论上是每天晚上要跑一次 E2E，E2E 通过之后，那么就更新版本。如果说目前这个 E2E 的测试还没有办法每次都通过，那么就一直跟随 Stable 版本就可以了。
> 请根据这个逻辑修改 release CI，Stable 和 nightly 落后的版本手动推送，0.5.5.1 发布要检测是否更新到了 stable track 和 nightly track

2026-10-26 人工补齐记录（研发负责人提供，非本改动的实测）：此前 stable
停在 `3c79d6163`（0.5.0，10-05），nightly 停在 `bbe80d440`（0.4.6，09-30）。
旧 SOP 第 4 步依赖人工，0.5.1–0.5.5 未推进 stable；nightly 未再获得同 SHA
双绿。研发负责人已将两轨移到 `v0.5.5`（`5dc2d1c95`）。internal 不变。
本改动不执行生产补推；0.5.5.1 的实际发布与公网同步仍须另验。

## 2026-09-27 安装源与锁定规则

- 持久升级源固定为 `https://github.com/Hyprial/hyprial.git`。PEP 610
  `direct_url.json` 只报告当前安装来自哪里，不参与下一次升级的选源。
- 每次安装前都从选定源抓取解析到的确切 commit，在临时 checkout 内执行
  `uv export --frozen --no-hashes --no-emit-project --no-dev`，并把结果以
  `uv tool install --constraints <file>` 传入。抓取、`uv.lock` 或导出任一步
  失败都会终止；没有未锁定回退。
- `hyprial daemon upgrade --source forgejo --ref <完整提交或标签>` 是一次性的早期
  测试入口。它把安装钉到解析 commit，但不写持久源；下一次普通升级或自动
  更新仍检查公网发布仓。
- 当早期测试版本高于公网版本时，无人值守自动更新与普通升级都报告
  `declinedDowngrade` 并跳过。确需回退时，操作员显式使用公网 `--tag`；显式
  tag 保留既有允许回退语义。
- `hyprial version` 分别显示持久升级源、PEP 610 当前安装来源，以及当前环境
  是否符合上次安装时从该 commit 锁文件导出的 pins。旧安装没有锁回执时明确
  报 `unknown`，不伪装成匹配。

三个可移动 tag 的语义、唯一写入方、main 发布链与并发红线统一见
[发布规则](release-rules.md#1-四条规则与执行机制)，不在本文重复定义。

## 客户端行为(PR-C,`src/hyprial/updates.py`)

- `settings.json` `updateTrack` 是轨道选择器:`internal` / `nightly` / `stable`
  各解析同名可移动 tag;**缺省见下文「缺省翻转」**。
- track 安装钉解析到的确切 commit(`git+url@<commit>`):可移动 tag 在
  解析与 uv 抓取之间可能移动;且钉 tag 名会让 uv 记录的 PEP 610
  `requested_revision` 与 restart 判定式错位,导致每次 timer tick 都重启
  daemon —— 两条劣化都有回归测试钉死。
- track tag 缺失 = `UPGRADE_CHECK_FAILED`,**绝不 fallback** 到最高版本
  tag;非法值 / 坏 settings.json 同样 fail-loud 不猜。
- `hyprial daemon upgrade --tag <t>` 是人工逃生门,永远优先于 track 配置。
- ⭐ `settings.json` `autoUpgrade`(布尔,2026-09-15 Allen 定案)是**自动
  升级总开关,缺省 = 关**:未显式 `hyprial config set autoUpgrade true`
  的机器,daemon 调度器到点只记 `autoupdate.run.skipped`
  (`reason=disabled`)不起子进程;隐藏的 `upgrade run` 只接受 daemon 调度器
  子进程路径,daemon 不在线时直接跳过;`upgrade status` 的
  `autoUpgradeEnabled` 读出开/关。
  手动 `hyprial daemon upgrade` 不受此开关约束。开启后另有两道防线:本地
  (`file://`/路径/editable)安装源拒绝升级(`UPGRADE_LOCAL_SOURCE`),
  以及 `uv tool install` 调用点校验本进程位于 uv 将写入的 tool 目录内
  (`UPGRADE_TOOL_DIR_MISMATCH`,解析不出 fail-closed)。
- daemon 自动更新调度器(每日 03:17 / 15:17 本地)跑 `hyprial daemon upgrade`,
  轨道经 settings.json 生效。旧 launchd/systemd 单元在 daemon 启动时迁移删除。

## ⛔ 三条硬规矩(本轮实证得出,违反会摧毁体系地基)

### 规矩 1 —— 晋升判定只能比 commit,绝不能比版本串

任何"这台机器要不要更新 / 这条轨要不要前进"的判定,**只允许拿
「上次记录的 commit」和「tag 当前指向的 commit」比对**。

⚠️ **拿版本串比大小会给出无意义的答案**,实证:

```
0.4.2.dev7  <  0.4.2  <  0.4.2+gd09e912  <  0.4.2.post1  <  0.4.3.dev1
```

- 带 local 段的**恒排在纯版本之后** ⇒ 每个打了戳的 wheel 都"比正式版新";
- **两个不同 commit 的 wheel 之间按 hash 字典序**:`+gzzzzzzz > +gaaaaaaa`,
  而它们的**时间先后毫无关系**。

### 规矩 2 —— `+g<hash>` 只用来钉 commit,不用来判新旧

`pyproject.toml` 永远是手工声明的 `X.Y.Z`;`+g<short7>` **只存在于构建产物**,
作用是"这个 wheel 出自哪个 commit",**不承载任何顺序语义**。

⚠️ `0.4.2.<hash>` 这种写法**在 PEP 440 里非法**(实测 `InvalidVersion`)。

点号后只能跟 PEP 440 认识的**预/后发布段**(`aN` / `bN` / `rcN` / `postN` / `devN`),
**且它们之间有顺序约束** —— 实测:

```
0.4.2.post1.dev1   合法        0.4.2.dev1.post1   非法
0.4.2.rc1.dev1     合法        0.4.2.abc123       非法
```

⇒ **任意 hash 一律必须用 `+`(local segment),不存在任何用点号的合法写法。**

### 规矩 3 —— registry 拒收重复版本是**防覆盖特性**,不是故障

同一 tag 对同一 SHA 重跑 CI,发布步会**响亮失败**。那是设计,不是 bug。

```
✅ 正确处置:让 tag 钉一个新 SHA,再发。
⛔ 绝不:删除已发布的正式版本再重传。
```

**为什么这条要写在这里而不是留给常识** —— 因为"删了重传"是**遇到 duplicate
拒绝时最顺手的动作**,而它摧毁的是「**版本 ↔ commit 一一对应**」这个整个三轨
体系赖以成立的地基:一旦同一个版本号先后对应过两个 commit,
上面规矩 1 和 2 的所有推理同时失效。

⚠️ **即使在允许删除的地方(仅 `0.0.x` 实验命名空间),方法也只有一个**:
`tea api --method DELETE`(它会正确转义 `+`)。**curl 手拼 `+` 或 `%2B` 均 404**
——不要在实验空间练出错误的肌肉记忆再带到正式版上。

> **出处**:这一条来自 PR #212 作者(`pkg-forge`)的 registry spike 记录,
> **未经独立复核**(复核需要真的删除一个已发布的包)。其余各条均已独立实测。

---

## 发版 SOP(stable 轨)

先完成 [发布检查模板](release-checklist-template.md)，再按
[发布规则的完整链路与三方验收清单](release-rules.md#2-完整发布链)执行。
从 0.5.5.2 起不再手工在 dev 头上打正式 tag；规则与例外只在该文档定义。

## 机队 track 策略(ops 决策项,Allen 裁定)

- 默认建议:全员 `stable`;追随每日验证的机器设 `nightly`;仅 hyprial 开发机
  设 `internal`（跟随 dev，可能未过 E2E）。
- 设置方式:直接编辑目标机 `~/.hyprial/settings.json` 加
  `"updateTrack": "<track>"`(无 CLI 写路径,`hyprial set update_track` 已
  移除)。下一次 timer tick(最迟 12h)或手动 `hyprial daemon upgrade` 生效。
- **切换语义**:换轨后解析到的 commit 与已装不同即重装 —— 包括"向
  后"移动(如从 internal 轨退回 stable 轨会装回 stable 指向的较早 commit)。
  这是治理意图内的行为,不是故障。

## Bootstrap(三 tag 初始化,一次性)

- `internal`：由现有 dev 更新流程自动建立。
- `stable`：首次正式 v* 发布时自动建立（见 [发布规则](release-rules.md)）。
- `nightly`：正式发布时由 stable 保底自动建立；也可由双闸同绿晋升建立。
  不需要等待首次双绿才能跟随已发布 stable。

## 历史记录：缺省翻转（三 tag 就位后的收尾小 PR）

过渡态(PR-C 合入时):**缺省 = 旧"最高版本 tag"行为,逐字节等价**,
现网 autoupdate 零变化。三个 tag 全部建立后,由一个小 PR 把缺省翻为
`stable`(`read_update_track` 缺省返回 `"stable"` + 测试翻转):

- 翻转日仍在 legacy 语义的机器:若已装版本领先于 stable(如
  `0.4.2.devN`),解析 stable commit ≠ 已装 commit → 重装 stable 指向的
  commit。同「切换语义」,这是治理意图:**未 opt-in 的机器锚定正式发
  版**。
- 不希望被锚定的机器必须在翻转前显式设 `nightly` 或 `dev` —— 翻转 PR
  的发布说明必须带这条行动号召,并给出一个完整 timer 周期(12h)的
  预告窗口。
