# hyprial-dl — 发布产物分发面（dl.hyprial.com）

对外只做两件事：把**已发布**的产物按 tag 原样发出去，并回答"当前最新版是什么"。
⛔ 不构建、不签名、不改字节 —— 这个 Worker 是分发层，不是发布层。

约束来源：`group-internal/decisions/D-infra-ops-0002.md` §3（R2 公开读规则）；规划见
`hq/notes/plan-homepage-and-releases-on-cf-20261023.md`。

## 路径布局（⛔ 改动前先读下一节）

```
/wheel/<tag>/SHA256SUMS      ← 安装器入口：SUMS 必须与 wheel 同目录
/wheel/<tag>/<file>.whl
/desktop/<tag>/SHA256SUMS    ← 桌面资产与其 SUMS 同目录
/desktop/<tag>/<file>
/latest.json                 ← 由 meta/latest.json 生成；⛔ 安装器不得引用
/install.sh · /install.sh.sha256  ← 安装脚本（桶根 key），**逐条精确**放行
/install.ps1 · /install.ps1.sha256
```

### 根安装脚本（2026-10-26）
四个路径是**字面量白名单**，不是根目录放行：⛔ 通配、⛔ 后缀匹配、⛔ 编码变体
（`/install%2Esh` 也是 404 —— 一个对象只能有一个 URL）。其余根路径仍 404。
缓存与 `SHA256SUMS` 同档（`max-age=60, must-revalidate`）：`install-scripts` 重跑会就地覆盖同名 key，
⛔ 不得跟着 wheel 走 `immutable`。

### ⚠️ 为什么 SUMS 必须与资产同目录（2026-10-23 实测）
`install.sh:198-202` 与 `install.ps1:241` 都按**SHA256SUMS 所在目录**推导资产 URL：

```sh
_BASE_URL=$(printf '%s' "$_SUMS_ONLY" | sed 's#/[^/]*$##'); WHEEL_URL="$_BASE_URL/$_PICK_ENC"
```
```powershell
$dlBase = ([uri]$SumsUrl).AbsoluteUri -replace '/[^/]*$', ''
```

把 SUMS 与 wheel 放在不同前缀（例如 `/sums/<tag>/SHA256SUMS` + `/wheel/<tag>/*.whl`）会让安装器
请求 `/sums/<tag>/xxx.whl` ⇒ 404 ⇒ `wheel_download_empty` ⇒ **静默回落源码安装**。
用户侧表现为"迁移完还是慢"，而安装收据里 `method` 会如实写 `source`，**看起来一切正常** ——
这是本仓最贵的一条坑，改布局前先跑一遍端到端安装。

## 行为约定

| 项 | 约定 |
|---|---|
| 方法 | 只允许 `GET` / `HEAD`，其余 `405` + `Allow` |
| 未知路径 | **`404` 纯文本**，不触达 R2（不泄露某个对象是否存在），且不带服务器指纹。⚠️ **404 只表示「白名单未命中」**；白名单命中但 R2 返回 null 一律 `503` + `retry-after`（2026-10-28T10:28–10:30Z 两件 manylinux 的瞬态 404 就是这个形状：该路径不经边缘缓存，只能是 `get()` 当时返回了 null；而 404 会让安装器**静默回落源码安装**，用户侧只表现为“迁完还是慢”） |
| 缓存 | 不可变路径 `max-age=31536000, immutable`；`latest.json` `max-age=60, s-maxage=60` |
| 目录/列表 | ⇢ 不存在。路径必须"知道 tag 就能构造" |
| 前导斜杠 | 只接受单个 `/`；`//wheel/…` 与 `/wheel/…` 不会同时可达（一对象两 URL 会让缓存与审计分叉） |
| `..` / `.` / 空段 | 一律 `404` |
| 段数 | **恰好三段** `/wheel/<tag>/<file>`：更深一个段就是 404（否则「白名单」这个词不成立，键空间无界） |
| Range | 四种情形分开答（222MB 的 dmg 要按 `content-range` 归位）：**①** 范围**由 `head()` 取到的对象大小本地判定**：不可满足 ⇒ `416` + `content-range: bytes */<size>`，且**不向 R2 取对象**；**②** 范围可满足且 R2 给了片段 ⇒ `206` + `content-range` + 片段 `content-length`；**③** R2 忽略了 range（返回对象但不带 `.range`）⇒ `200` + 整份、⛔ 不带 `content-range`（「206 配整份」会让续传**静默错位**）；**④** 其余存储故障（含 `head()` 自身抛、含白名单命中但对象为 null）⇒ `503` + `retry-after: 1`、⛔ 416（416 只表示「范围不存在」）、⛔ 回显内部错误（旧版曾把不可满足 range 的 R2 异常直接漏出去 ⇒ CF 1101 / HTTP 500，2026-10-27 线上实测）。畸形/多段 `Range`（`bytes=abc`、`bytes=0-1,4-5`、`items=…`）按 RFC 9110 **忽略**、回 `200` 整份 |
| `r2.dev` | **禁用**。公开读只经 `dl.hyprial.com` |
| `/sums/…` | **不存在**（不是 302、不是镜像）：安装器按**配置里那个 URL 字符串**推目录，第二份 SUMS URL 会推成 `/sums/<tag>/<whl>` ⇒ 404 ⇒ 静默回落源码 |
| 根安装脚本 | 只放行 `/install.sh` `/install.sh.sha256` `/install.ps1` `/install.ps1.sha256` 四个字面量；其余根路径 404，且不触达 R2 |
| `+` 文件名 | 路径必须 percent-decode：`%2B` 与裸 `+` 解析到**同一个**对象键；而**除 `%2B` 以外的百分号编码一律 404**（`%2E` 会把 `a%2Ewhl` 与 `a.whl` 映到同一对象 = 白送第二个 URL；顺带盖住 `%2F`/`%5C`/`%20`） |
| 发布 | **不可逆**：已发布路径永不覆盖，新内容 = 新路径 |

## 本地校验（零依赖，不需要 `npm install`）

```bash
node --test test/*.test.ts        # 25 条
```

依赖 Node 的 TS 类型剥离：**在 Node v26.4.0 上实测通过（25/25），更低版本未测**。

## 部署

```bash
# ⚠️ 只用绝对路径 CLI（本仓工具链口径：禁 npx、禁 npm run deploy:*、禁局部 node_modules 的 wrangler）
/opt/homebrew/bin/wrangler deploy --dry-run --outdir /tmp/hyprial-dl-dry   # 先证明能编译
/opt/homebrew/bin/wrangler deploy                                          # 生产：需逐次授权
```
`account_id` 由 `CLOUDFLARE_ACCOUNT_ID` 环境变量注入，⛔ 不写进仓。
`compatibility_date` 取**当前 workerd 上限**（2026-10-23 实测 `2026-07-07`；`2026-07-08` 被 workerd 拒绝）。
上限随 wrangler/workerd 版本变化 ⇒ 要动先报「当前 runtime + 上限 + 理由」，⛔ 不得顺手改成未来日期。

## 部署后探针（缺一条就不算完成）

1. `curl -sSI https://dl.hyprial.com/latest.json` ⇒ 200 + `cache-control: max-age=60`；
2. 任取一个 wheel：`HEAD` ⇒ 200，`content-length` 与源文件**同字节**；`sha256` 与 SUMS 条目一致；
3. `curl -r 0-1023 -o /dev/null -w '%{http_code}'` ⇒ 206；
4. `/`、`/wheel/`、`/sums/<tag>/x.whl`、`/api/.env` ⇒ **404 纯文本**，正文与头部无 `nginx` / `wrangler` / `workerd`；
   另：`/install.sh` `/install.sh.sha256` `/install.ps1` `/install.ps1.sha256` ⇒ 200 且 `cache-control` **不含** `immutable`；
   其余根路径（`/env.sh`、`/install.sh.bak`）⇒ 404；
5. `dig dl.hyprial.com` ⇒ 回读实际记录并与计划稿对账。
