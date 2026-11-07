# Spec — 边车获取从 `install` 搬到 `login`

- **Unit**: `sidecar-via-login` · **Author**: infra-op · 2026-09-11
- **Requested by**: Allen, 2026-09-11(飞书原话):
  > 将 sidecar 下载的功能从 hyprial install 的路径中移除,sidecar 的下载路径应该是 hyprial login,
  > 1. 用户在执行 hyprial login 时,如果发现没有安装 sidecar 或者版本有升级,提示它下载安装
  > 2. 可以使用 hyprial login --install-sidecar 跳过 y/n 确认
  > 3. 也可以使用 hyprial login --only-install-sidecar 仅安装 sidecar 不登录
- **Repo**: 本仓(`HyprialOS/harness-bridge`)
- ✅ **Status(2026-09-12 更新)**:**已实现并已合入 dev** —— 实现者是 PR **#463**
  (`login: move the sidecar acquisition step under hyprial login`),合入 dev 的 merge commit =
  **`2da21807`**。
  ⇒ 📌 **本文现在的定位是【设计档案】**,⛔ 不是待办 —— §1/§2/§3/§6 记录的是**为什么这样做**
  (三条命令形态、三处已定决策及其理由、同意形态不可动、§6 的结构前提),那些仍然有效。
  ⇒ ⛔ **不要把它读成派单书** —— 🔑 这份文档上一版的 Status 写着「派单用 spec,infra-op 不实现」,
  而实现早已落地;⚠️ 照那行字去做会**重做一遍已经合入的东西**。
  ⇒ ⭐ 未做项:**无**。§4 的 D/E/F 已由 #463 满足(逐条读数见 §4)。
- **Moved**: 2026-09-11 由 hq `notes/` 迁入本仓 `docs/`(Allen 指示:spec 应与代码同仓)。
  ⇒ 📌 hq PR #66 因此关闭,⛔ 不是被否决 —— ⭐ 它换了位置。
- ⚠️ **与 (丙)/(丁) 那条迁移线无依赖** —— 📌 但**排在它后面做**:
  🔑 那条有 15:17 的硬窗口,而本单没有。⛔ 不要让本单的测试红拖住那个 PR。

---

## 0. ⭐ 为什么要搬 —— 三个**已实测**的缺陷,⛔ 不是偏好

> ⭐⭐ **本节全部读数的基线,钉死如下(2026-09-12 补)。**
> 📌 §0 描述的是 **#463 之前**的状态,所以它的行号与计数**只在那个基线上成立** ——
> ⛔ 在今天的 dev 上重跑会全部不命中,而那**不是本节出错**,是它描述的缺陷**已经被修掉了**。
> ```
> 基线 commit = 4da3a0c8b808…  (= 2da21807^1,即 #463 合入前的 dev 头)
>   src/hyprial/tsnet_join.py                     blob 7034dbcadd4c…
>   src/hyprial/cli.py                            blob e4c3df0955da…
>   contract/e2e/05-uv-install-flow.md  blob 1ff826aa01a5…
> 复核方式(与机器/cwd/checkout 无关):git show "<blob 或 sha>:<path>"
> ```
> ✅ 本节每一处引用都已在该基线上**逐条机械断言过**(下面每小节标了结果)。
> 📌 对照:**今天的 dev**(`76bba4ca`,blob `tsnet_join.py` = `752774811216…`,
> `cli.py` = `b128724ad04f…`,`05-uv-install-flow.md` = `a34d0a344044…`)上的实测是
> 「`run: hyprial install` 0 命中 / `install_sidecar()` 唯一调用点在 `cli.py:771`(login 侧)/
> `05-uv-install-flow.md` 中 `sidecar` 3 命中」⇒ ⭐ 即三条缺陷都已消失。

### 0.1 ⛔ 今天的提示语是一句**照做必失败**的指令,而它有【两个独立病因】
```
src/hyprial/tsnet_join.py:582 · :590 · :634 ⇒ 三处都写 "run: hyprial install"
✅ 基线上已断言:该字符串在 tsnet_join.py 中**恰好 3 次**,且就在 :582 / :590 / :634
病因一:⛔ 命令缺必填参数
   cli.py:2711  name: str = typer.Argument(..., help="Application name from the trusted catalog.")
   ⇒ 🔑 `hyprial install` 不带 app 名 ⇒ 直接报参数缺失
病因二:⛔ 生产上 `hyprial` 这个命令**还不存在**(改名尚未到达各机器)
   ⇒ 🔑 所以今天连命令名本身都是错的
⇒ ⭐ 两个病因**互不相关**,📌 修一个不修另一个,提示仍然是坏的
```

### 0.2 ⛔ 要一个【网络组件】,必须先装一个【GUI 应用】
```
install_sidecar() 在全仓只有一处调用点:cli.py:2806,在 `install` 命令体内(✅ 基线上已断言)
⇒ 而 catalog 里唯一的条目是 `gui`
⇒ ⇒ 🔑 于是"取边车"的唯一办法是 `hyprial install gui` —— ⛔ 语义上说不通
📌 历史原因在代码注释里写着(cli.py:2775):
   「the pinned hyprial-tsnet sidecar **rides the same consent shape**」
   ⛔ **更正(2026-09-12)**:上一版把这条注释标为 `cli.py:2775` ⇒ 基线上实测它在 **`cli.py:2776`**
   (:2775 是空行)⇒ 📌 差一行。✅ 同基线上 `:2806` = `result["sidecar"] = install_sidecar(` 与
   `:2711` = 那个 `typer.Argument(...)` 两处**逐字命中**。
   ⇒ ⭐ 即:边车搭了 install 已有的"同意形态"的车,⛔ 而不是它属于 gui
```

### 0.3 ⛔ 全新安装那条 e2e **完全没有覆盖边车获取**
```
contract/e2e/05-uv-install-flow.md ⇒ grep sidecar / 边车 / install gui ⇒ 0 命中
⇒ 🔑 也就是:**这条路从来没有被量过** ⇒ ⭐ 而它正是新人唯一会走的那条
```

## 1. ⭐ 要建什么(按 Allen 的三条)

```
① `hyprial login` ⇒ 发现边车【缺失】或【与 pin 不符】时,**提示并在同意后安装**,然后继续登录
② `hyprial login --install-sidecar`      ⇒ 跳过 y/n(⭐ 同意形态的内容仍要打印,只是不问)
③ `hyprial login --only-install-sidecar` ⇒ **只装边车,不做登录**
```
⚠️ ②③ 互斥性与组合要明写:`--only-install-sidecar` 是否隐含 ②?⇒ ⭐ 承接方定并在 PR 写明理由。

## 2. ⛔ 三处我已经替承接方定了,⭐ 连理由一起,⛔ 不要重新开放
```
(a) **无 TTY 时不提示、不挂住**
    ⇒ 按现状报 SIDECAR_MISSING / SIDECAR_MISMATCH,并在提示里指向 `--install-sidecar`
    🔑 理由:提示在无人处等于挂死 —— 📌 我们已在 120 秒设备码上栽过一次
(b) **`--only-install-sidecar` 不要求已登录**
    🔑 理由:取边车走 `git archive --remote`(ssh 凭据),⛔ 与 OIDC 身份无关
(c) **`install` 那一段整段移除,⛔ 不留"兼容性保留"**
    🔑 理由:留着就有两条取边车的路 ⇒ ⇒ 📌 而两条路必然漂移 ——
       ⭐ 今天 §0.1 那条死提示就是漂移的现货
```

## 3. ⛔⛔ 不许动的那部分:**同意形态本身**

```
🔑 现在的形态(cli.py:2775-2805 注释与实现)是故意的,⭐ 搬家时必须原样带走:
  ① 下载前先亮:来源仓 + tag + 版本 + 期望 sha256
  ② 只有 sha256 校验通过才落地 ⇒ 原子 rename 进 $HYPRIAL_HOME/bin/hyprial-tsnet
  ③ 不匹配 ⇒ 不留半个文件
  ④ 用户拒绝 ⇒ 跳过该步,⛔ 不把整条命令判失败
⇒ ⛔ 任何"简化同意"的改动一律退回 —— 📌 那是这条链唯一的信任根
```

## 4. ⭐ 验收(⛔ 每条都要读数,不接受"应该可以")

```
A 三条形状各跑一次,并贴原文:
  ① 无边车 ⇒ login ⇒ 提示 ⇒ y ⇒ 边车落地 ⇒ 登录继续
  ② --install-sidecar ⇒ ⛔ 不问 y/n,⭐ 而同意形态的内容仍被打印
  ③ --only-install-sidecar ⇒ 边车落地 ⭐ 且【没有】发生登录(⛔ 要有证据:secrets/login.json 未被写)
B ⭐ 负控制:把 pin 的 sha256 改错一位 ⇒ ⇒ 必须 SIDECAR_SHA_MISMATCH 且 bin/ 下无残留
C ⭐ 无 TTY:在无 tty 的环境跑 login ⇒ ⛔ 不得挂住 ⇒ 报 SIDECAR_MISSING 且提示里含 --install-sidecar
D ⭐ `install` 侧:确认 install_sidecar 的调用点为 **0**(⛔ 不是"注释掉")
   ⇒ 并且 `hyprial install gui` 仍然正常装应用(⛔ 不得因移除而回归)
   ✅ **已满足(#463)**:今天的 dev 上 `install_sidecar(` 的调用点只有 **`cli.py:771`**
      (login 的 `acquire_sidecar`),`install` 命令体内 **0 命中**;
      `tsnet_sidecar.py:273` 是 `def`,⛔ 不是调用点
E ⛔ §0.1 那三处提示必须一起修好:
   ⇒ 提示里给出的命令,**逐字复制粘贴就能跑通**(⭐ 含 app 名/新命令名两个病因都修掉)
   ✅ **已满足(#463)**:今天的 dev 上 `run: hyprial install` **0 命中**;
      三处已改为 `run: hyprial login --install-sidecar`(:577 · :590 · :638),另有三处 hint 同改
F ⭐ 补 `contract/e2e/05-uv-install-flow.md`:把边车获取写进全新安装那条
   🔑 理由:§0.3 —— 它现在零覆盖,而修完若仍零覆盖,下一次照样没人知道它坏了
   ✅ **已满足(#463)**:今天的 dev 上该文件里 `sidecar` **3 命中**(:29 · :30 · :45),
      都是 `hyprial login --only-install-sidecar`
```

## 5. ⛔ 不在本单
```
⛔ (丙)/(丁) 的包名与 uv tool 迁移 —— ⭐ 另一条线,有 15:17 硬窗口
⛔ 删除 migrate-from-ts —— ⭐ 单独开单(引用面 11 文件,含专守改名契约的那条测试)
⛔ 改动 catalog(让 catalog 里多几个 app)—— 🔑 与本单无关
⛔ 放宽同意形态 —— 见 §3
```

## 6. ⚠️ 一个承接方很可能撞上的坑
```
🔑 `login` 现在的结构是:身份段(OIDC device flow)⇒ 入网段(tsnet join)
⇒ ⭐ 而边车校验发生在**入网段的开头**(基线 `4da3a0c8` 上:**:574 起**,
   `sidecar_path = sidecar_binary_path(home)`;T9 的那段注释在 :570–:571)
   ⛔ **更正**:上一版写的是 `:577 起`,而基线上 :577 是 `except Exception:` ⇒ 📌 指到了块中间
⇒ ⇒ 📌 所以"提示安装边车"放在哪里,决定了 `--only-install-sidecar` 能不能**跳过身份段**
⇒ ⇒ ⇒ ⛔ 若实现成"走完身份段再装边车",那 ③ 就做不到"不登录"
⭐ 所以:边车这一步必须能**独立于身份段**被调用 —— 🔑 这是 ③ 的结构前提,不是优化
```
