# Wheel 分发链路（Forgejo CI → R2 / packages / GitHub）

`internal`、`nightly`、`stable` 发布时，Forgejo CI 构建带 commit 戳的
五平台 wheel，先直接发布到 `dl.hyprial.com` 的 R2，再发布到 Forgejo
packages（PyPI registry，HyprialOS org）及 GitHub release。
公网安装器从 R2 的 `SHA256SUMS` 选择并校验，不经过 GitHub 下载。

- 状态:spike 判据 **GO**(2026-08-20,Forgejo 15.0.5 实证,见下文)
- 交界:tag 移动逻辑(nightly-promote / 人工 SOP)归 up-gate;本链路只消费
  tag-push 事件,不触碰 `updates.py`/autoupdate/任何 tag 逻辑。

## Public one-line install

macOS (arm64/x86_64) and Linux (x86_64/aarch64):

```sh
curl -fsSL https://dl.hyprial.com/install.sh | sh
```

Windows amd64, from PowerShell:

```powershell
irm https://dl.hyprial.com/install.ps1 | iex
```

The default track is `stable`. Select another track explicitly:

```sh
curl -fsSL https://dl.hyprial.com/install.sh | HYPRIAL_TRACK=nightly sh
```

```powershell
$env:HYPRIAL_TRACK = 'nightly'; irm https://dl.hyprial.com/install.ps1 | iex
```

Only `stable`, `nightly`, and `internal` are accepted. Stable follows formal
releases; nightly is E2E-passed or follows stable; internal follows dev. The scripts
contain no package version pin. They fetch `wheel/<track>/SHA256SUMS`, require
exactly one platform match, and verify SHA-256 before `uv tool install --force`.
Missing manifests (including an unpopulated track), ambiguous matches, bad hashes
and failed installs stop the script. There is no source or alternate-track fallback.
The current wheel tags require macOS 13+ or glibc-based Linux; Windows uses amd64.

If uv is absent, the script first prints its official installer URL and target
folder, then bootstraps uv from `astral.sh`. The target is `UV_INSTALL_DIR`, or
`~/.local/bin` by default. `UV_UNMANAGED_INSTALL` avoids modifying shell profiles.
Re-running replaces the tool environment, even if a different version is installed;
it does not restart a running daemon. `uv tool list` must confirm the selected
version. A schema:1 receipt is written to
`${XDG_CONFIG_HOME:-$HOME/.config}/hyprial/install.json` (same convention on Windows).
If needed, run `uv tool update-shell` and open a new terminal to expose the CLI.

`HYPRIAL_DL_BASE` is an isolated-test override, not a production mirror selector.
Changing it changes the trusted source of both the manifest and wheels. SHA-256
checks consistency with that source; it is not an independent release signature.
The public script itself can also be downloaded and inspected before execution;
its companion `.sha256` object contains the script hash.

## Direct R2 publication and drift check

`install-scripts.yml` runs on relevant `dev` changes and manual `dev` dispatches.
It generates and uploads only `install.sh`, `install.sh.sha256`, `install.ps1`,
and `install.ps1.sha256`, each script before its checksum. The separate `drift`
job runs afterward even if publication failed; it compares anonymous public reads
with the checkout bytes. The drift command needs no R2 credentials:

```sh
uv run --no-project --python 3.12 python scripts/ci/publish_installers.py --check
```

Checkout still uses the CI job's repository token; the drift HTTP requests are
unsigned. A mismatch or inaccessible object fails with `dl out of sync with repo`.
No periodic schedule is configured; use an explicit dev dispatch for a fresh check.

`wheel-release.yml` calls `publish_wheels.py` immediately after local build and
verification, before registry publication or GitHub asset attachment. It rejects
incomplete, mixed-version, duplicate-platform or bad-hash sets before touching R2.
Publication requires `--source-sha` (the full immutable build commit). Before any
upload and immediately before each manifest PUT attempt, including retries, the
publisher resolves the track's peeled commit from source remote `origin`. Missing,
unreadable or superseded refs fail closed; mismatch reports
`stale run: <track> now at <sha>`. No local tag or GitHub mirror is used as authority.
A mid-run move can leave candidate wheels, but the stale run does not switch SUMS.
These checks are not an atomic Git-ref/R2 transaction; a move after the final
check remains a race window.

Each of the five wheels is uploaded and read back anonymously before
`wheel/<track>/SHA256SUMS` is uploaded and verified. Old wheels remain available;
there is no automatic deletion. This replaces infra-ops' GitHub-to-R2 wheel mirror
as the normal publication path. Until the new CI succeeds for a track, that track
can still be absent and installation must fail rather than choose another track.

Both lanes use `scripts/ci/r2_upload.py` (stdlib SigV4) and the existing CI secrets
`R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Missing values
fail with `R2 credentials not configured`. No credentials enter argv, logs or files.
Objects use `Cache-Control: max-age=60, must-revalidate`; shell scripts use
`text/x-shellscript`, PowerShell/checksums use `text/plain`.

A paginated full-bucket ListObjectsV2 snapshot precedes and follows every upload
attempt, including partial failures. The guard compares keys, ETags and sizes;
only the four installer keys, or the current `wheel/<track>/` prefix, may change.
This is detection, not a bucket transaction: concurrent writers outside the scope
also fail the guard, and failures do not roll back uploaded objects. Do not run
concurrent publication lanes if a clean guard result is required. Rerun after
resolving the cause. Each request has a 15-second socket timeout and at most three
attempts (10s/60s delays); workflow deadlines bound the full job. Public read-back
uses fresh unsigned requests and checks exact bytes, not S3 ETags as SHA-256.

Real R2 publication and real installations on each OS require separate acceptance;
local S3/uv fixtures do not establish those results.

## 版本方案:PEP 440 本地段

仓库 `pyproject.toml` 永远只携带手工声明的基版 `X.Y.Z`;构建产物额外打
**本地版本段** `+g<short7>`,使命名到确切 commit:

```
pyproject: 0.4.2          →  wheel: hyprial-0.4.2+g8cd97b4-py3-none-any.whl
```

- 戳只存在于构建产物;注入用 `uv version "<基版>+g<short7>" --no-sync`,
  本地构建后必须恢复工作树(`scripts/build/build-wheel.sh` 的 trap 模式),
  CI 容器一次性则无需恢复。
- Forgejo registry 已实证**完整保留** `+` 本地段,且
  `uv pip install 'hyprial==0.4.2+g8cd97b4'` 精确 pin 命中(Gitea 系早期版本
  对 `+` 的规范化坑在 15.0.5 不复现)。
- registry **拒收重复版本文件**:同一 tag 重复钉同一 SHA 的 CI 重跑会在
  发布步响亮失败 —— 这是防覆盖的正确行为,换 SHA 再发。

## 构建

本地(手工/实验):

```bash
scripts/build/build-wheel.sh [outdir]   # trap 恢复 pyproject/uv.lock;树脏会警告
```

CI(`.forgejo/workflows/wheel-release.yml`):

- `nightly`/`stable`:由 `on.push.tags` 的字面 tag push 触发。
- CI 自己写 tag 不会再触发 tag-push workflow。`tests.yml` 在核对 internal
  指针后直接 dispatch `wheel-release`，再 dispatch 独立的 `github-publish`；
  GitHub 失败不阻塞 R2。正式版本的 stable/nightly writer 和 nightly 晋升
  同样先派发 wheel，再派发 GitHub。三轨均传轨道名和已固定的完整 `source_sha`。
- 构建对象:push 事件用 `github.sha`;三轨 dispatch 用输入的完整
  `source_sha`。两条路径都 checkout 该 SHA 并核对 HEAD,**绝不重新解析
  tag 名**(构建中途 tag 可能再次 force-move,重解析 = 半态)。
- `v*` 不构建 wheel(见文末 open question)。
- 并发:`concurrency: wheel-<track>`,`cancel-in-progress: false`
  (发布半截取消会在 registry 留脏态)。
- 步骤:checkout 事件 SHA → 装 uv 0.11.7 → preflight 校验 secret 存在 →
  stamp + `uv build --wheel` → 校验并直接发布 R2 → `uv publish`。

## 发布

上传端点与认证:

```
URL:  https://code.hyprial.com/api/packages/HyprialOS/pypi   (uv publish / twine 通用)
auth: basic = PKG_PUBLISH_USER : PKG_PUBLISH_TOKEN
```

CI job 只按名引用 repo secret `PKG_PUBLISH_USER` / `PKG_PUBLISH_TOKEN`
(经 `UV_PUBLISH_URL/USERNAME/PASSWORD` 环境变量传入,凭据只活在进程
env:不进 argv、不落盘、不进任何持久化 URL)。**workflow 与文档零
secret 值。**

## 装回(消费端)

registry 读侧需认证(org packages 未开放匿名读),依赖仍需回落 PyPI:

```bash
uv venv ~/.venvs/hyprial   # 或任何隔离位置;实验不要动 uv tool 全局位
UV_INDEX_URL="https://${PKG_PUBLISH_USER}:${PKG_PUBLISH_TOKEN}@code.hyprial.com/api/packages/HyprialOS/pypi/simple/" \
UV_EXTRA_INDEX_URL="https://pypi.org/simple" \
uv pip install --python ~/.venvs/hyprial/bin/python 'hyprial==0.4.2+g<short7>'
```

- 精确 pin 本地段保证装回的就是那个 commit 的构建,不受后续同基版发布
  影响。
- 凭据经 env 注入;不要写进 shell rc、netrc 或任何会被提交的脚本。
- 若日后 org packages 开放匿名读,消费侧凭据面归零(Allen 裁定项)。

## 凭据面(谁持有什么,零值)

| 凭据 | 形态 | scope | 持有/配置方 | 消费方 |
|---|---|---|---|---|
| `PKG_PUBLISH_TOKEN` | Forgejo access token(pkg-bot) | package write+read | Allen → repo secret | wheel-release.yml 发布步 |
| `PKG_PUBLISH_USER` | 字面 `pkg-bot`(非密) | — | 同上 | 同上 |
| 消费侧 read 凭据 | 同上 token 或低配 read token | package read | 各消费端自行注入 env | 装回命令 |

GitHub attach 还需要已有 repo secret `GH_PUBLISH_TOKEN`(目标仓 Contents RW);
内部 workflow dispatch 使用已有 `GITHUB_TOKEN`。`internal` 不需要
`ATTACH_WHEEL_ASSETS` repo variable;它是强制路径。该 variable 只控制既有
`nightly`/`stable` attach。

红线:任何 agent 不得自行寻找/铸造 token;发现凭据缺失只报需求。

## GitHub internal release 的完整集与两步指针切换

GitHub release asset 名字唯一,也没有 rename-over 或多对象事务,所以这里
**不承诺原子替换**。`attach_wheel_assets.py` 先从 registry 取齐并校验**恰好
五个**平台文件,再上传带版本戳的五个候选 wheel 和临时名
`SHA256SUMS.<version>`;每个上传对象都从 GitHub 读回并核对。候选集完整前,
旧 `SHA256SUMS` 不动。

切换窗口明确为两步:先把旧指针改名为 `SHA256SUMS.previous-<asset-id>`,再把
已验证的 staging manifest 改名为 `SHA256SUMS`。因此两次 rename 之间会短暂
没有 canonical `SHA256SUMS`;每次 rename 和 rollback 都按同一网络策略最多
尝试 3 次,退避 10 秒、30 秒。第二步失败而 rollback 成功时,run 置红且旧完整
集合重新成为 canonical。第二步与 rollback 都失败时,run 以 rc=4 置红,输出
`CANONICAL SHA256SUMS IS MISSING`,并明确列出 backup asset 与不含凭据的
`python3 scripts/ci/attach_wheel_assets.py ...` 修复命令;此时不清理任何旧资产。

每次重跑在候选上传或清理之前先自修复:若 canonical 缺失,就检查所有
`SHA256SUMS.previous-*` 和 staging manifest,逐个验证其引用的五个 wheel 与
SHA-256,再把 asset id 最新的完整 manifest 恢复成 `SHA256SUMS`。所以构建
失败、registry 不全或候选上传/验证失败时旧指针不变;指针切换后的旧文件清理
失败也会置红,但新完整集合已被选择,重跑继续清理。

`dl.hyprial.com` bucket 归 infra-ops。此前由 infra-ops 从 GitHub `internal`
release 镜像；现在由本仓 CI 直接发布三轨。以下是 internal 的六个对象示例，
nightly/stable 使用各自同名目录（文件名以当次 `SHA256SUMS` 为准）：

```text
dl.hyprial.com/wheel/internal/SHA256SUMS
dl.hyprial.com/wheel/internal/hyprial-<version>+g<sha7>-py3-none-macosx_13_0_arm64.whl
dl.hyprial.com/wheel/internal/hyprial-<version>+g<sha7>-py3-none-macosx_13_0_x86_64.whl
dl.hyprial.com/wheel/internal/hyprial-<version>+g<sha7>-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
dl.hyprial.com/wheel/internal/hyprial-<version>+g<sha7>-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
dl.hyprial.com/wheel/internal/hyprial-<version>+g<sha7>-py3-none-win_amd64.whl
```

CI 先上传五个 wheel 并逐条匿名回读核对，最后替换 `SHA256SUMS`；
installer 从 SUMS URL 推导同目录，因此 manifest 是切换点。

### 0.5.4 `g14e3281` 的来源审计

已验证:`14e3281a392fa5ccfad2bb1f5926888dab18cef4` 是 2026-10-18 的
GitHub snapshot commit,其 commit message 记录原仓 source 为
`c5e0598ea2d662aceb2cc36e5ba3539d438f69b1`,并存在本地保留的
`wheel-probe-1792457744` tag;当前五个 internal wheel 的版本戳正是
`0.5.4+g14e3281`。同时,自动 attach 脚本到 2026-10-20 的 `b045b51c0`
才加入,且加入后的 workflow 只允许 `stable|nightly`,所以这些文件**不是**
当前 `wheel-release → github-publish attach` 自动路径产生。由 `wheel-probe`
命名与时间只能推断它们来自一次以公网 snapshot 为源的人工/probe 构建与
上传;仓库历史没有保存具体命令或 run id,不得把这一点写成已验证自动发布。
本次改动用显式 internal dispatch 取代该不可复现路径。

## 实验规程(不污染正式版本空间)

- 实验 wheel 一律 `0.0.x+<试验标记>` 版本(如 `0.0.1+spike1.g<hash>`),
  与正式 `X.Y.Z+g<hash>` 空间无交集。
- 实验包用完即删。**删除注意**:pypi 包版本含 `+`,curl 手拼 URL 的
  `+`/`%2B` 均会 404;用 `tea api --method DELETE
  /packages/HyprialOS/pypi/hyprial/<version>` 让客户端正确转义
  (2026-08-20 spike 实证)。
- 删后核验 `.../pypi/simple/hyprial/` 复空。

## Spike 判据存档(2026-08-20)

- 实验包 `hyprial-0.0.1+spike1.g8cd97b4`(dev @ 8cd97b4)上传/装回/删除全通;
- registry 版本字符串 `0.0.1+spike1.g8cd97b4` 一字未动;
- 装回用隔离 venv + 精确 pin,命中 Forgejo(实验版 PyPI 不存在,回落
  会直接 unsatisfiable,判据天然自证无假绿);
- 上传前置:发布账号须在具 packages write 的 team 内(否则 401
  reqPackageAccess)——不只 CI,手工上传同样需要。

## Open question(留 Allen 裁定)

正式发版的不可变 `vX.Y.Z` tag + release 对象是否另开一条 wheel 发布轨
(如在过滤中加 `v*` 或单独 workflow)?当前 `wheel-release.yml` 只服务
nightly/stable 轨;`v*` 不在其过滤内,up-gate 侧 `e2e.yml` 的 `v*` 闸与
本链路面无交集。
