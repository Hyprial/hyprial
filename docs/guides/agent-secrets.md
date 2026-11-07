# Agent 凭据：逐条导入、轮换与撤销

`hyprial agent secret` 把每个 agent 自己的凭据存入其 home 下的
`secrets.sqlite`。值是明文，数据库文件权限为 `0600`，agent home 权限为
`0700`。命令只从非 TTY stdin 取值；`list` 和 `history` 只输出元数据，不输出值。

本能力唯一的安全要求是**操作有记录**。本能力不提供权限系统或防篡改审计。

低熵值（密码、PIN 和非 token 形态的值）的 fingerprint 使用 agent home 下
`.secret-hmac-key` 的 HMAC；该 32 字节密钥在首次写入时生成，文件权限为 `0600`。
高熵 token 仍使用截短 SHA-256。升级前已有的 legacy-only 数据库会在首次
post-A3 `set` / `rotate` 时生成密钥，以便完成轮换。首次建立密钥会追加
`action=hmac_key_created` 事件，其中只保存 `sha256(K)[:16]`，不保存 K。事件建立后，
密钥缺失、权限不符、不可读，或密钥与事件不符时，写入以退出码 10 和
`FINGERPRINT_UNAVAILABLE` 拒绝，且不写入秘密行；不符时错误带 `key_mismatch`。
`list` / `history` 在密钥不可用时把 fingerprint 显示为 `unavailable`。

旧的 16 位十六进制 fingerprint 在 `history` 中标为 `legacy_sha`。旧的
`len=…,tail=…` 行标为 `legacy_plaintext_tail`，输出不再显示其中的尾段。指纹形态不参与
注入判定：有对应成功写入事件的 legacy 行仍可注入；防篡改检查只在该行自己的旧口径内
于内存重算，不跨口径比较，也不写出重算结果。事件表是 append-only，旧尾段无法删除；
owner 必须在首次发现后 7 天内轮换受影响的值。

## agent-private grant 的存储与迁移

`agent-private` grant 与 `set/rotate/remove` 使用同一份 SQLite 数据库。
`--entry-id` 对应 `--service`；`--field-name` 对应 `--field`。
写入值后，可按以下格式授权：

```bash
hyprial agent secret grant <agent> \
  --source agent-private --entry-id <service> --field-name <ENV_FIELD> \
  --environment-name <ENV_FIELD> --grant-id <grant-id> --revision 1
```

轮换后，下次解析使用新值。删除后，原 grant 解析失败。
缺值错误会给出带 agent、service、field 的补写命令。
值仍须从 stdin 输入，不得放入命令参数。

已有 grant 指向 `<agent home>/secrets/<entry-id>` 时，首次解析会迁移该文件：

1. 检查当前 agent 化身和 home 回执。
2. 检查旧文件类型、归属和权限。
3. 把完整文本和 JSON 字符串字段写入 SQLite。
4. 记录 `action=migrate`，每个导入字段记录 1 条。
5. 核对 SQLite 中的完整字段和值。
6. 把该文件的全部无字段 grant 绑定到完整文本字段。
7. 提交 grant 绑定后，删除旧文件。
8. 目录为空时，删除 `secrets/`。

迁移以源文件为单位，不受 grant 解析顺序影响。
完整文本默认存入 `VALUE` 字段。
若 JSON 已有同名键，则从 `LEGACY_RAW_1` 起选择首个未占用的键。
无字段 grant 的 revision 增加 1；已有字段绑定保持不变。
迁移不保留明文备份。中断后可重试，不重复写入迁移事件。
已更新的 grant 不会再次增加 revision。
若已有 SQLite 值与旧文件冲突，解析会拒绝，并保留旧文件。
操作者须先核对两份值的归属；不得删库来强行重做迁移。

新建 agent 不再生成空 `secrets/` 目录。
`secrets/native/{claude,codex,pi}/` 仍由第三方 harness 使用，本次不改。
名为 `native` 的普通文件不属于此目录，仍按旧凭据文件迁移。
未授权的旧文件不自动删除。home 级 login、provider、Lark 凭据文件也不改。

## 新建 Claude agent 的默认凭据

默认凭据只保存 owner 和条目引用。值只存于 owner 的 SQLite。
新建 Claude agent 时，系统原子写入 `default-claude` consumer grant。
CLI、PAC 和 lifecycle 创建使用同一规则。
CLI 创建时须传 `--preferred-harness claude`。
非 Claude agent、访客和 transfer 记录不自动获得授权。

1. 在可信终端运行 `claude setup-token`，取得长期 OAuth token。
2. 选择已有的 owner agent。
3. 在 host 操作员终端按下列命令导入并设为默认。

```bash
IFS= read -r -s VALUE
printf '\n' >&2
printf '%s\n' "$VALUE" | hyprial agent provider login claude \
  --setup-token --actor <owner> --default --json
unset VALUE
```

导入不验证厂商认证。不得把值写入 argv、日志或普通文件。
`--setup-token` 只接受非 TTY stdin，不能与 `--source` 同用。
替换已有 token 时须加 `--force`。
系统不为每个 agent 运行 `/login`。

```bash
hyprial agent provider default --json
hyprial agent secret ls <consumer> --json
hyprial agent secret revoke <consumer> default-claude
hyprial agent provider default --clear
```

`ls` 显示 grant；`list --agent` 显示本地值的元数据。
清除默认只影响后续创建，已发出的 grant 保留。
设置默认不会给已有 agent 补发 grant，也不会恢复已撤销的 grant。
已有显式 Claude auth grant 保持原样。

已有 agent 可单独获得共享授权：

```bash
hyprial agent secret grant <consumer> --source agent-shared --owner-agent <owner> \
  --entry-id claude --field-name CLAUDE_CODE_OAUTH_TOKEN \
  --environment-name CLAUDE_CODE_OAUTH_TOKEN --grant-id claude --revision 1
```

owner 轮换值后，下次解析使用新值；删除值或更换 owner 化身后，解析失败。
运行中的消费者须重启，才能观察轮换或撤销。
没有显式 auth 环境或自有私密 native 根时，P1 SDK 启动返回永久错误
`CLAUDE_CREDENTIAL_MISSING`。工作流所有或启动时创建的 agent，其 PTY 启动也执行此检查。
命令覆盖和交互式 CLI 不得绕过检查。系统不借用 daemon 登录，也不发起模型请求。
操作员创建的常驻 PTY 不因缺少凭据被拒绝，来源记录为 `ambient`。
由 daemon 组合环境的 P1 Claude 始终使用自有 native 根。
`CLAUDE_CONFIG_DIR` 指向 `<agent home>/secrets/native/claude`。
`projects` 链接指向 `<agent home>/state/sessions/claude`。
宿主的 selector 不会覆盖这两个路径，env grant 也使用同一路径。
常驻交互式 CLI 的凭据限制留待 0.5.5.2 secrets 改造。

新建的空根含 `.hyprial-launch-provisioned` 非秘密标记。
没有 env grant 时，带标记的根在重试后仍为 `missing`。
显式 `agent provider login` 原生安装成功后清除标记。
env grant 不清除标记；撤销授权后仍须重新满足来源检查。
不得手动删除标记来绕过检查。
已有的自有私密根不要求凭据文件存在。
这允许 Keychain-only 登录通过来源检查；系统不读取 Keychain。
文件存在时仍检查类型、权限和非空。来源检查不证明厂商认证成功。
PAC 节点的 `reasonRef` 为 `pac:launch_failed:CLAUDE_CREDENTIAL_MISSING`。
`worker.claude.credential_source` 只报告来源类别；`worker.readiness.waiting`
报告等待阶段。ready 不代表厂商认证成功。

### 原生登录复制与旧默认

`agent provider login claude --actor <owner> --default` 仍可复制原生登录。
新建 Claude agent 从该 owner 的登录副本复制，不读取 daemon 的宿主登录。
注意：把可刷新的 OAuth 登录复制给多个 agent，会使刷新相互干扰。
多个 agent 共用默认凭据时，宜使用 `--setup-token`。

旧版仅保存 provider 名称的默认设置仍支持 CLI 创建。
这种默认不支持 PAC 自动复制。须重新运行
`hyprial agent provider login claude --default --actor <owner>` 绑定来源。
默认回执中的 `defaultCredential` 只含 owner 化身和条目引用，不含值。
`provider default --clear` 同时清除旧默认名称和新来源引用。

## 从现存文件手动导入

对上述已授权文件之外的凭据，v1 **不自动扫描**，也**不自动迁移**。
旧文件里的值可能分别属于不同账号；
系统不能替 owner 判断身份归属。owner 必须核对 agent、服务和字段，然后逐条导入。
导入期间保留旧文件；确认受管行、事件和实际使用均正确后，确认后自行删除旧文件。
不要生成 `.bak`、`.retired` 或 `.old` 副本。

每个字段占 stdin 一行，值不得放入 argv。以下例子适合单字段、没有内置 validator 的
服务；`read -s` 避免终端回显，`--no-validate` 是 owner 对“只做形态、不探测能力”的
显式选择：

```bash
IFS= read -r -s VALUE
printf '\n' >&2
printf '%s\n' "$VALUE" |
  uv run hyprial agent secret set \
    --agent <agent> \
    --service <service> \
    --field <ENV_FIELD> \
    --no-validate \
    --json
unset VALUE
```

有内置 validator 的服务不要加 `--no-validate`。重复 `--field` 时，按参数顺序从
stdin 提供同样数量的行。例如 `cloudflare-r2` 需要
`CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_API_TOKEN`；`forgejo` 需要
`FORGEJO_BASE_URL` 和 `FORGEJO_TOKEN`。validator 只检查声明的字段和形态，不能证明
凭据可用。

每次 owner 导入都会留下 `actor=user:<owner>`、`via=cli`、`action=set` 的事件。
导入后先读元数据和事件：

```bash
uv run hyprial agent secret list --agent <agent> --service <service> --json
uv run hyprial agent secret history --agent <agent> --service <service> --json
```

再启动该 agent，完成一次所需服务的真实使用。只有这些检查完成，才删除原文件。
导入失败时保留原文件并修正输入；不要把“命令已执行”当成“凭据可用”。

## 轮换与本地删除

轮换也从 stdin 读取一个新值：

```bash
IFS= read -r -s VALUE
printf '\n' >&2
printf '%s\n' "$VALUE" |
  uv run hyprial agent secret rotate \
    --agent <agent> \
    --service <service> \
    --field <ENV_FIELD> \
    --no-validate \
    --json
unset VALUE
```

内置 validator 的服务同样省略 `--no-validate`。轮换覆盖当前行，不保留旧值副本；
事件保留旧、新 fingerprint 作为历史。只删除本地值时使用：

```bash
uv run hyprial agent secret remove \
  --agent <agent> --service <service> --field <ENV_FIELD> --json
```

## 撤销语义

撤销有两个独立动作：

1. 操作者在远端服务中使凭据失效，随后记录 `action=revoke_remote`；
2. 删除本地行，记录 `action=remove`。

只有两条事件都存在，撤销才完成；完成态必须是**值不在盘上**。`secrets` 表没有
revoked 状态，也没有撤销后的保留窗口。回滚窗口应放在远端撤销之前，而不是靠保留
已失效值实现。

CLI 不调用远端服务。完成远端操作后，操作者用下列命令声明远端撤销事实并立即删除
本地值：

```bash
uv run hyprial agent secret remove \
  --agent <agent> \
  --service <service> \
  --field <ENV_FIELD> \
  --remote-revoked \
  --remote-revoked-at 2026-10-22T12:00:00Z \
  --json
```

如果中断后只出现 `revoke_remote` 而没有 `remove`，该行仍存在、仍可物化。这是可查询的
未完成态，不是安全完成态。`list` 和 `history` 会把该状态报告为
`remote_revoked_value_present`。任何事件推导存在性的读路径都只能认 `remove`，不得因
`revoke_remote` 断言值已清理。

## 威胁模型

同 uid 是已知边界：操作系统允许同 uid 进程直接读取其他 agent home 中的明文。
`path_authz` 约束的是 daemon 代读代写和路径交付，不拦截进程自己 `cat` 文件。
数据库内的 append-only 触发器也不等于外部、防篡改审计；删除整个
`secrets.sqlite` 会同时删除事件。

以下文本是本能力的固定边界说明，必须原样理解：

```text
防：跨 uid / daemon 代读代写与路径交付 / 命令面留痕
不防：同 uid 直接读、读取不产生事件
不含：独立 uid、keychain ACL、值加密、异地审计
```

v1 明确不做：权限模型、批准流程、外部审计、文件交付、值加密、定时轮换和能力探测。
一份数据库只存该 agent 自己的凭据。跨 agent 使用必须有 `agent-shared` grant。

## Validator 与能力探测

validator 只判断字段是否齐全、值的形态是否符合声明。v1 不实现 `capability_probe`；
创建成功不代表凭据可用，元数据读取成功也不代表目标操作获准。以后若实现探测，结果
至少分成三类，不能压成成功/失败二分类：

- `credential-invalid`：凭据本身无效；
- `target-missing`：凭据有效，但目标不存在；
- `operation-not-permitted`：凭据有效且目标存在，但不允许该操作。

读数纪律是：列表 ≠ 存在、创建 ≠ 可用、可解析 ≠ 合法、凭据可用 ≠ 目标存在。

## 验收 1–9 可运行清单

下表按规格顺序映射到 pytest node id。8b 保留为 8 的独立阻断检查。

| 验收 | 可运行测试 node id |
| --- | --- |
| 1 | `tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_1_set_stores_value_but_never_puts_it_in_events` |
| 2 | `tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_2_update_event_is_aborted_by_append_only_trigger`<br>`tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_2_delete_event_is_aborted_by_append_only_trigger` |
| 3 | `tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_3_rotate_changes_current_fingerprint_and_keeps_history` |
| 4 | `tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_4_remove_stops_injection_and_keeps_events` |
| 5 | `tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_5_sql_row_without_write_event_is_rejected` |
| 6 | `tests/identity/agents/configuration/test_agent_secret_identity_policy.py::test_wp1_reserved_name_rejection_carries_the_required_event`<br>`tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_6_reserved_sql_row_is_never_injected` |
| 7 | `tests/shell/cli/agents/secret/test_commands.py::test_acceptance_7_unknown_service_refuses_without_override_and_counts_override`<br>`tests/shell/cli/agents/secret/test_commands.py::test_acceptance_7_validator_is_declarative_shape_only` |
| 8 | `tests/shell/cli/agents/secret/test_commands.py::test_revoke_flow_records_assertion_and_idempotently_resumes`<br>`tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_8_remote_revoke_remains_materializable_until_remove` |
| 8b | `tests/shell/cli/agents/secret/test_commands.py::test_list_and_history_expose_incomplete_remote_revoke`<br>`tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_8b_event_and_table_reads_agree_remote_only_is_present` |
| 9 | `tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_9_rotation_and_completed_revoke_leave_no_value_copy` |

从仓库根目录运行整个清单：

```bash
uv run pytest -q \
  tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_1_set_stores_value_but_never_puts_it_in_events \
  tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_2_update_event_is_aborted_by_append_only_trigger \
  tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_2_delete_event_is_aborted_by_append_only_trigger \
  tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_3_rotate_changes_current_fingerprint_and_keeps_history \
  tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_4_remove_stops_injection_and_keeps_events \
  tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_5_sql_row_without_write_event_is_rejected \
  tests/identity/agents/configuration/test_agent_secret_identity_policy.py::test_wp1_reserved_name_rejection_carries_the_required_event \
  tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_6_reserved_sql_row_is_never_injected \
  tests/shell/cli/agents/secret/test_commands.py::test_acceptance_7_unknown_service_refuses_without_override_and_counts_override \
  tests/shell/cli/agents/secret/test_commands.py::test_acceptance_7_validator_is_declarative_shape_only \
  tests/shell/cli/agents/secret/test_commands.py::test_revoke_flow_records_assertion_and_idempotently_resumes \
  tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_8_remote_revoke_remains_materializable_until_remove \
  tests/shell/cli/agents/secret/test_commands.py::test_list_and_history_expose_incomplete_remote_revoke \
  tests/identity/agents/home/migration/test_sqlite_secret_launch.py::test_acceptance_8b_event_and_table_reads_agree_remote_only_is_present \
  tests/identity/agents/secret_storage/test_secret_store.py::test_acceptance_9_rotation_and_completed_revoke_leave_no_value_copy
```
