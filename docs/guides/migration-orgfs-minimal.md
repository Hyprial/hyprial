# OrgFS 精简实现迁移指南

> 本指南写给执行迁移的 agent 或操作员。产品代码不包含任何迁移、转换或兼容读取：升级后的
> hyprial 不读旧格式的 OrgFS 空间，也不改动它们。旧数据要带过来，只能按本指南在升级**前**
> 用旧版本导出、升级**后**用新版本重新写入。CHANGELOG 中 OrgFS 精简实现一条链接到这里。
>
> 本指南的步骤在三台隔离的测试节点上按原文实际走过一遍（旧版本导出、升级、新建并写入、校验、
> 重新邀请、checkout、删除旧目录、回退）。仍标注“未验证”的地方没有经过实测，执行时以实际输出
> 为准，结果与本文不符时停下来记录，不要猜。

## 1. 先弄清几个词

- **OrgFS**：hyprial 的组织文件空间。每个**空间**（space）有一个 UUID 形式的空间 ID、一个名字、
  一个 owner 和一张成员表；空间里是目录和文件**节点**（node），每个节点有自己的节点 ID。
- **文本文档**（`kind: doc`）：用 `org fs write` 写入、可以继续编辑的文本。
- **二进制文件**（`kind: blob`）：用 `org fs import` 导入的文件内容，不可编辑，只能整体替换或删除。
- **org 目录空间**：每个组织自带的一个空间，名字就是组织名（`group-<名字>`），保存设备记录、
  成员与离开请求。由组织流程创建和维护，不由用户直接写。
- **ACL 空间**：每个组织的另一个保留空间，名字是组织名加 `-acl`。同样由组织流程创建。
- **holder**：持有某个空间副本的节点（一台运行 daemon 的机器）。

## 2. 谁需要迁移

升级前这台机器上有任何 OrgFS 空间的安装都需要迁移。检查方法（daemon 运行时，用旧版本）：

```sh
hyprial org fs space list --json
```

`spaces` 为空的安装不需要做任何事，直接升级即可。

在磁盘上，旧格式的空间是 `<state>/orgfs/spaces/<spaceId>/` 下带 `journal.sqlite3` 的目录
（`<state>` 默认是 `~/.hyprial/state`；设置了 `HYPRIAL_HOME` 时是它下面的 `state`，设置了
`HARNESS_STATE_DIR` 时以它为准）：

```sh
STATE="${HARNESS_STATE_DIR:-${HYPRIAL_HOME:-$HOME/.hyprial}/state}"
ls "$STATE"/orgfs/spaces/*/journal.sqlite3 2>/dev/null
```

本指南里凡是用到 `STATE` 的代码段都用这一行定义它：没有设置 `HYPRIAL_HOME` 时（默认安装就是
这样）取 `~/.hyprial`。不要把 `HYPRIAL_HOME` 设为空串。

## 3. 改了什么，哪些不兼容

新版 OrgFS 只保存每个文档的**当前状态**，不再保存逐次提交的日志与历史。随之而来：

- **磁盘布局不兼容。** 新格式的空间目录里是 `space.json`（`layoutVersion: 3`）和若干
  `*.state` / `*.delta` 文件，没有 `journal.sqlite3`。新版本遇到旧格式目录时只拒绝这个空间，
  对它的请求返回 `ORGFS_LAYOUT_UNSUPPORTED`，错误数据里有 `spaceId`、`reason`（旧格式为
  `legacy-journal`）和指向本指南的 `guide`。其他空间照常打开，daemon 照常启动，旧目录的字节
  不会被改动。
- **线路协议不兼容。** 新旧版本之间的 OrgFS 同步不互通，新节点与旧节点之间不交换任何空间数据。
  **同一组织的所有节点要在同一个窗口里一起升级**；窗口期间先升级的节点与尚未升级的节点之间
  文件不同步。
- **删除的功能**：节点历史（`org fs history`，以及按版本读取的 `orgfs.read_at`、
  `orgfs.stat_at`）；全网内容清除（`org fs purge plan`、`org fs purge status`、
  `org fs purge unban`）；常驻持有方（`org fs space serve`）。
- **改变的命令**：
  - `org fs trash restore SPACE NODE` 不再接受版本参数。它恢复被删节点删除前的最后状态，
    以及与它在同一次删除里一起删掉的后代；被覆盖之前的旧正文无法恢复。
  - `org fs rm` 新增 `--permanent`：从当前状态彻底删除节点及其后代（只有空间 owner 或该节点的
    作者可以这样做）。不加时仍是放进回收站。
  - `org fs space status` 的结果字段改为 `space_id`、`synced`、`holders_online`、
    `content_pending`、`rejected_updates`；原来的 `unconfirmed_commits`、`durable_holders_seen`
    等字段删除。`org network` 结果里 `directoryPending` 的 `parkedCommits` 字段删除。
- **保留的**：本机空间清除 `org fs purge run SPACE [--yes]` 保留（但它对旧格式空间同样返回
  `ORGFS_LAYOUT_UNSUPPORTED`，不能用来删旧目录，见第 10 节）。

## 4. 不会带过来的东西

按本指南迁移后，下列内容**不会**出现在新空间里。需要的话在导出时留底（第 5.2 节）。

| 内容 | 结果 |
| --- | --- |
| 历史版本与提交记录 | 丢失。新空间里每个文件只有导入时的一个当前版本 |
| 回收站 | 丢失。导出只走活动的目录树，新空间回收站为空 |
| 成员表 | 丢失。新空间只有创建者一个成员（`rw`），其余成员要重新邀请（第 6.4 节） |
| 作者与修改时间 | 丢失。新节点的作者是执行导入的人，时间是导入时刻 |
| 空间 ID、节点 ID | 改变。新空间与新节点都是新 ID；所有按 ID 引用的东西（`orgfs:` URI、外部链接、checkout）都要改 |
| checkout（本地只读投影） | 不迁移。要按新空间 ID 重新开启（第 6.5 节） |
| 导不出的二进制文件 | 丢失。本地内容已被清除（`purged`）、本地没有内容且没有在线 holder（`content-pending`）、或超过 64 MiB（`too-large`）的文件无法导出 |
| 文本文档的“可编辑”身份 | 只有按第 6.3 节用 `org fs write` 写入才保留；用 `org fs import` 导入的文本会变成不可编辑的二进制文件 |
| org 目录空间、ACL 空间 | 不按本指南迁移（第 7 节） |

在一份约 2.6 万个文件的真实数据克隆上演练过这一流程（进程内调用与 CLI 相同的 daemon 请求，
不是逐条运行 CLI）：普通空间的全部可导出文件逐字节一致、路径与目录集合一致；新进程打开 20 个
空间不到 1 秒。逐条运行 CLI 时每条命令约 0.18 秒（主要是进程启动），2.6 万个小文件仅导出一项就
要一个多小时，写入另计；大空间请预留小时级的窗口。

## 5. 升级前：用旧版本导出

在**旧版本**、daemon 正常运行时做。每个空间只需要在一个节点上导出，选该空间内容最全的节点，
最好是空间 owner 自己的节点（导入时由谁创建新空间，谁就是新 owner）。

### 5.1 准备工作目录与备份

`org fs export` 只能写到当前工作目录或工作区之内，不能写进 hyprial home 或 state 目录。

```sh
mkdir -p ~/orgfs-migration/export && cd ~/orgfs-migration
hyprial org fs space list --json > spaces.json
jq -r '.spaces[] | "\(.space_id)\t\(.name)\t\(.owner)"' spaces.json
```

升级前还要整体备份 OrgFS 数据目录，它是回退（第 9 节）的唯一依据。先停 daemon，再复制：

```sh
STATE="${HARNESS_STATE_DIR:-${HYPRIAL_HOME:-$HOME/.hyprial}/state}"
BK="$HOME/orgfs-backup-$(date +%Y%m%d)"
if [ -d "$STATE/orgfs/spaces" ] && [ ! -e "$BK" ]; then
  hyprial daemon stop && { cp -a "$STATE/orgfs" "$BK"; rc=$?; hyprial daemon start
    [ "$rc" -eq 0 ] && echo "backup ok: $BK" || echo "backup FAILED (cp exit $rc)"; }
else
  echo "stop: no $STATE/orgfs/spaces, or $BK already exists"
fi
```

只有看到 `backup ok` 才继续升级。看到 `stop:` 时先弄清原因：`$STATE` 指的不是实际的数据目录，
或者同名备份已经存在（不会覆盖）。

### 5.2 每个空间：留底成员表与回收站

```sh
SPACE=<旧空间 ID>
hyprial org fs member list "$SPACE" --json > "members-$SPACE.json"
hyprial org fs trash list "$SPACE" --json > "trash-$SPACE.json"
```

回收站列表默认有条数上限；条目多时加 `--limit <N>`。

### 5.3 每个空间：逐层遍历并导出

`org fs ls` 只列一层，需要脚本逐层遍历。下面的脚本把每个节点记进 `nodes-<SPACE>.tsv`（种类、
路径、节点 ID），并把每个文件导出到 `export/<SPACE>/<路径>`。字段名 `.nodes[]`、`.kind`、
`.path`、`.node_id` 在旧版本与新版本上都已实测可用。先对一个小空间运行，核对输出再处理大空间。
脚本只导出文件，不在 `export/` 下建空目录；空目录记录在 `nodes-<SPACE>.tsv` 的 `dir` 行里，由
第 6.2 节重建。本指南的脚本都按 bash 写。

```sh
SPACE=<旧空间 ID>
: > "nodes-$SPACE.tsv"; : > "failed-$SPACE.tsv"
walk() {
  hyprial org fs ls "$SPACE" "$1" --json |
    jq -r '.nodes[] | [.kind, .path, .node_id] | @tsv' |
    while IFS=$'\t' read -r kind path node; do
      printf '%s\t%s\t%s\n' "$kind" "$path" "$node" >> "nodes-$SPACE.tsv"
      if [ "$kind" = dir ]; then
        walk "id:$node"
      else
        dest="export/$SPACE/$path"
        mkdir -p "$(dirname "$dest")"
        hyprial org fs export "$SPACE" "id:$node" "$dest" --wait 10 --json \
          < /dev/null > /dev/null 2>> "failed-$SPACE.log" ||
          printf '%s\t%s\n' "$path" "$node" >> "failed-$SPACE.tsv"
      fi
    done
}
walk ""
```

- 导出失败的文件记在 `failed-<SPACE>.tsv`，原因在 `failed-<SPACE>.log`。`purged`、
  `content-pending`、`too-large` 三种原因的文件无法迁移（第 4 节）；`content-pending` 的文件
  可以换一个已有内容的节点再导出，或让持有它的节点在线后用更长的 `--wait`（最多 120 秒）重试。
- 导出完成后记录核对基准（macOS 用 `shasum -a 256`，Linux 用 `sha256sum`）：

```sh
(cd "export/$SPACE" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256) \
  > "sha256-old-$SPACE.txt"
```

## 6. 升级后：新建空间并写入

同一组织的所有节点都升级完成后再做。对旧空间的读写都返回 `ORGFS_LAYOUT_UNSUPPORTED`，这是
预期的。升级后旧空间**不再出现**在 `org fs space list` 里，旧组织也不出现在 `org list` 里；
要处理哪些空间以第 5.1 节的 `spaces.json` 为准。升级后如果看到一个名为 `hyprial-home` 的新空间，
它是本机收件通知用的，不是遗漏的旧空间。

### 6.1 确认旧空间被拒绝

```sh
hyprial org fs ls <旧空间 ID> --json
# 预期：退出码 1，ok:false，code ORGFS_LAYOUT_UNSUPPORTED，data.reason legacy-journal，
# data.guide 指向本指南。不带 --json 时输出一行以 "hyprial: ORGFS_LAYOUT_UNSUPPORTED:" 开头的说明。
```

### 6.2 新建空间与目录

对每个**普通**空间（跳过 org 目录空间和 ACL 空间，见第 7 节）：

```sh
OLD=<旧空间 ID>
NEW=$(hyprial org fs space create "<原空间名>" --json | jq -r '.space_id')
printf '%s\t%s\n' "$OLD" "$NEW" >> space-map.tsv
awk -F'\t' '$1=="dir"{print $2}' "nodes-$OLD.tsv" | sort |
  while IFS= read -r dir; do hyprial org fs mkdir "$NEW" "$dir" --json < /dev/null > /dev/null; done
```

`space create --json` 输出中新空间 ID 的字段名是 `space_id`。
`sort` 让父目录排在子目录之前。

### 6.3 写入文件：文本用 `write`，二进制用 `import`

**文本文档必须逐个用 `org fs write` 写入。** `org fs import` 总是把文件存成不可编辑的二进制，
之后对它 `org fs write` 会返回 `invalid-argument: target is not a text document`。

把文件内容作为命令行参数传给 `org fs write` 时，要保证一个字节都不变。shell 的 `"$(cat f)"`
会去掉末尾的换行，导致哈希不一致；下面的写法在末尾补一个哨兵字符再去掉，从而保留末尾换行：

```sh
OLD=<旧空间 ID>; NEW=<新空间 ID>
while IFS=$'\t' read -r kind path node; do
  src="export/$OLD/$path"
  [ -f "$src" ] || continue
  case "$kind" in
    doc)
      text=$(cat "$src"; printf x); text=${text%x}
      hyprial org fs write "$NEW" "$path" "$text" --json < /dev/null > /dev/null ;;
    blob)
      hyprial org fs import "$NEW" "$path" "$src" --json < /dev/null > /dev/null ;;
  esac || printf '%s\t%s\n' "$kind" "$path" >> "import-failed-$OLD.tsv"
done < "nodes-$OLD.tsv"
```

注意命令行参数的长度限制：

- 单个文本文档的上限是 4 MiB。
- macOS 上全部参数与环境变量合计不能超过 1 MiB；Linux 上**单个参数**不能超过 128 KiB
  （131071 字节可以，131072 字节报 `Argument list too long`）。超过的文本无法这样经命令行
  写入。`org fs write` 不读标准输入，也没有从文件读正文的参数（把 `-` 当正文传会写出一个
  内容为 `-` 的文档）。这类文件只能先用 `org fs import` 存成二进制（内容不丢，但不可编辑），
  并在迁移记录里列出。
- 二进制文件的导入上限是 64 MiB。

### 6.4 重新邀请成员

按留底的成员表逐个邀请，跳过你自己。成员表里的 `user` 带 `user:` 前缀，原样传给邀请命令。
“你自己”取新空间成员表里创建者那一行（`added_by` 等于自己的那一行），这样循环中途失败后可以
原样重跑。对方在自己的节点上加入：

```sh
ME=$(hyprial org fs member list "$NEW" --json |
  jq -r '[.members[] | select(.user == .added_by) | .user] | if length == 1 then .[0] else error("creator not unique") end')
[ -n "$ME" ] || echo "stop: cannot tell which member is you"
jq -r --arg me "$ME" '.members[] | select(.user != $me) | "\(.user)\t\(.mode)"' "members-$OLD.json" |
  while IFS=$'\t' read -r user mode; do
    hyprial org fs member invite "$NEW" "$user" --mode "$mode" --no-notify --json < /dev/null
  done
hyprial org fs member list "$NEW" --json      # 以成员表为准，核对每个人与模式
# 在成员自己的节点上：
hyprial org fs space join <新空间 ID>
```

这里用 `--no-notify`，新空间 ID 由你另行告知成员。不加它时，邀请命令会等对方节点的回执，实测
在对方在线的情况下也可能返回 `ok:false`、`USER_DELIVERY_TIMEOUT` 和非零退出码，而成员其实已经
加上（已知问题 KP-152）；所以无论哪种写法，都以 `member list` 的结果判断成功与否，不要以邀请
命令的退出码判断。原来的加入时间不会保留。

### 6.5 重建 checkout 与更新引用

- 原来开启过 checkout 的空间，按新空间 ID 重新开启：`hyprial org fs space checkout <新空间 ID> --enable`
  。投影在 `<state>/orgfs/spaces/<新空间 ID>/checkout`，其中的文件与目录都是只读的。
- 把 `space-map.tsv` 发给使用这些空间的人与 agent。凡是写着旧空间 ID 或旧 `orgfs:` URI 的
  地方（文档里的链接、脚本、agent 的配置）都要改成新 ID；节点 ID 也都变了，按路径重新解析：
  `hyprial org fs resolve <新空间 ID> "<路径>" --json`。

## 7. org 目录空间与 ACL 空间

这两类空间不按本指南迁移：

- **ACL 空间**（名字以 `-acl` 结尾）不能用 `org fs space create` 重建，会返回
  `ORGFS_ACL_SPACE_EXTERNAL_MUTATION`；它们只由组织流程生成。迁移它们的内容**不受支持**。导出的
  文件只作留底。
- **org 目录空间**（名字就是组织名）不导入。设计上的做法是组织的各节点在升级后重新加入组织，
  由组织流程重新生成目录空间。做法：owner 在升级后的节点上重新建立组织
  （`hyprial org create <组织名>`），再给每个成员生成邀请（`hyprial org invite <组织名> --user <用户>`），
  成员用 `hyprial org join <邀请链接>` 加入。旧目录空间仍在磁盘上时可以用同一个组织名重建，
  得到新的目录空间与 ACL 空间。这一步在测试节点上走通过，但测试环境的登录身份是测试替身；
  在真实登录身份下执行前与组织 owner 确认窗口，并把实际结果记下来。

## 8. 校验

对每个新空间，用新版本把全部文件再导出一遍，和第 5.3 节的基准比较哈希与路径集合：

```sh
OLD=<旧空间 ID>; NEW=<新空间 ID>
while IFS=$'\t' read -r kind path node; do
  [ "$kind" = dir ] && continue
  [ -f "export/$OLD/$path" ] || continue
  dest="readback/$OLD/$path"; mkdir -p "$(dirname "$dest")"
  hyprial org fs export "$NEW" "$path" "$dest" --json < /dev/null > /dev/null ||
    echo "readback failed: $path"
done < "nodes-$OLD.tsv"
(cd "readback/$OLD" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256) \
  > "sha256-new-$OLD.txt"
diff "sha256-old-$OLD.txt" "sha256-new-$OLD.txt" && echo "PASS $OLD"
```

`diff` 没有输出即文件的哈希与路径集合都一致。这一步只比较文件；空目录另行核对。只看
`resolve` 的退出码不够：同一路径上是文件而不是目录时它也返回成功，所以要看返回的 `kind`：

```sh
awk -F'\t' '$1=="dir"{print $2}' "nodes-$OLD.tsv" | while IFS= read -r path; do
  hyprial org fs resolve "$NEW" "$path" --json < /dev/null 2>/dev/null |
    jq -e 'any(.nodes[]?; .kind == "dir" and .deleted != true)' > /dev/null || echo "missing dir: $path"
done
```

另外用 `org fs space status <新空间 ID> --json` 看
`content_pending` 为 0；有其他 holder 在线时 `synced` 应为 `true`，没有任何 holder 在线时为
`null`。

## 9. 回退

- **还没升级任何节点**：什么都不用做，导出文件只是副本。
- **已经升级、要回到旧版本**：
  1. 在新版本上把迁移后又写入新空间的内容按第 5.3 节导出（备份里没有这些改动）。
  2. 停 daemon：`hyprial daemon stop`。
  3. 把当前的 OrgFS 目录移开，换回升级前的备份：

     ```sh
     STATE="${HARNESS_STATE_DIR:-${HYPRIAL_HOME:-$HOME/.hyprial}/state}"
     BK="$HOME/orgfs-backup-<日期>"
     if [ -d "$STATE/orgfs" ] && [ -d "$BK/spaces" ]; then
       mv "$STATE/orgfs" "$STATE/orgfs.after-upgrade-$(date +%Y%m%d)" && cp -a "$BK" "$STATE/orgfs" &&
         echo "restore ok"
     else
       echo "stop: no $STATE/orgfs, or no backup at $BK"
     fi
     ```

  4. 按原来的安装方式装回升级前的版本，再 `hyprial daemon start`。
  5. 同一组织的其他节点也要一起回退，否则新旧节点之间不同步。

  在测试数据上，旧版本能启动并忽略混放其中的新格式空间目录；但二进制内容的存储与引用计数是
  新旧共用的，混放后的引用计数没有评估，所以这里仍然整体换回备份，不要把新旧目录混放。
  移开的 `orgfs.after-upgrade-<日期>` 里如果有开过 checkout 的空间，其中的目录是只读的，删除
  前先对这一个目录执行 `chmod -R u+w`。

## 10. 什么时候可以手工删除旧目录

旧格式目录不会被自动删除，`org fs purge run` 对它们也返回 `ORGFS_LAYOUT_UNSUPPORTED`，只能手工
删除。同时满足下面几条后再删：

1. 每个普通空间都按第 8 节校验通过；
2. 同一组织的所有节点都已升级，成员确认能正常使用新空间；
3. 确定不再回退，且第 5.1 节的备份已另存到别处。

```sh
STATE="${HARNESS_STATE_DIR:-${HYPRIAL_HOME:-$HOME/.hyprial}/state}"
[ -d "$STATE/orgfs/spaces" ] || echo "stop: no $STATE/orgfs/spaces"
hyprial daemon stop
for d in "$STATE"/orgfs/spaces/*/; do
  [ -f "$d/journal.sqlite3" ] && echo "$d"
done
# 逐个核对上面列出的目录确实是要删的旧空间，再用字面路径删除：
rm -rf "${STATE:?}/orgfs/spaces/<旧空间 ID>"
hyprial daemon start
```

只删 `spaces/` 下的旧空间目录。`$STATE/orgfs/blobs/` 与 `blob-refs.sqlite3` 由新空间共用，
不要删。删除之后，旧空间 ID 返回 `unknown-space`，不再是 `ORGFS_LAYOUT_UNSUPPORTED`，这是预期的。
开过 checkout 的旧空间目录里有只读的目录，`rm -rf` 报 `Permission denied` 时先对这一个目录
执行 `chmod -R u+w "${STATE:?}/orgfs/spaces/<旧空间 ID>"` 再删。

删掉旧空间目录后，只被旧空间引用的二进制内容**不会被回收**：它们留在 `blobs/` 里，
`blob-refs.sqlite3` 里也留着已删空间的记录，目前没有命令可以清理。这只占磁盘空间，不影响使用。
`$STATE/orgfs/` 下的 `replica/`、`purge-intents/`、`deleted-orgs/` 新版本不再使用，删除它们
是否安全没有评估，**未验证**（测试数据里没有这些目录），留着不影响新版本运行。
