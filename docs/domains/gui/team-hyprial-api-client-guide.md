# Codex / Claude Code / Pi 接入 api.hyprial.ai 团队指南

更新日期：2026-09-23。本文面向需要在 Codex CLI/Desktop、Claude Code 或 Pi 中使用
Hyprial 网关的团队成员。文中命令已按以下版本核验：

- Codex CLI `0.155.1`
- Claude Code `2.1.278`
- Pi `0.84.2`
- `@dailin521/codex-provider-sync` `0.5.0`

## 1. 网关能力与密钥

`https://api.hyprial.ai` 是多协议 LLM 中转网关，不是普通的 Hyprial 业务 REST
API。当前三类常用协议如下：

| 客户端 | 协议 | 请求地址 | 客户端应填写的 Base URL |
| --- | --- | --- | --- |
| Codex | OpenAI Responses | `https://api.hyprial.ai/v1/responses` | `https://api.hyprial.ai/v1` |
| Pi | OpenAI Chat Completions | `https://api.hyprial.ai/v1/chat/completions` | `https://api.hyprial.ai/v1` |
| Claude Code | Anthropic Messages | `https://api.hyprial.ai/v1/messages` | `https://api.hyprial.ai`，不要带 `/v1` |
| 模型发现 | OpenAI 风格目录 | `https://api.hyprial.ai/v1/models` | `https://api.hyprial.ai/v1` |

网关接受 `Authorization: Bearer`、`x-api-key` 和 `x-goog-api-key`。Codex、Pi 和
Claude Code 的本指南统一使用 Bearer 密钥。

密钥按账号 group 动态授权模型，不要把某个成员看到的模型清单写死到共享配置。先用
自己的密钥查询实际目录：

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'

curl -sS https://api.hyprial.ai/v1/models \
  -H "Authorization: Bearer $HYPRIAL_API_KEY"
```

2026-09-23 当前密钥示例目录有 14 个 DeepSeek 模型，包括
`deepseek-flash`、`deepseek-v4-pro`、`deepseek-v4-flash-vision-exp`、
`deepseek-v3`、`deepseek-r1` 等。其他密钥的目录可能不同，以 `/v1/models`
实时结果为准。

模型命名说明：本文示例统一使用当前口径 `deepseek-flash`。`deepseek-flash` 和
`deepseek-v4-flash` 均已计划未来退役；正式替代模型和迁移日期公布前，团队新配置统一
使用 `deepseek-flash`，届时再按统一公告迁移。

## 2. Codex 接入

### 2.1 推荐：使用独立 profile，不污染根配置

把 Hyprial provider 只放进 `~/.codex/hyprial.config.toml`。Codex 的
`--profile hyprial` 会把这份文件叠加到根 `~/.codex/config.toml` 上，根配置中的默认
provider/model 保持不变。推荐用 `env_key` 引用环境变量，不要把真实 key 写进 TOML：

```toml
model_provider = "hyprial"
model = "deepseek-flash"
model_reasoning_effort = "max"
disable_response_storage = true

[model_providers.hyprial]
name = "Hyprial API"
base_url = "https://api.hyprial.ai/v1"
env_key = "HYPRIAL_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
```

确认启动 Codex 的 shell 能读到密钥：

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'
codex --profile hyprial
```

需要临时改用其他模型时：

```bash
codex --profile hyprial -m deepseek-v4-pro
```

最小验收：

```bash
codex --profile hyprial exec --skip-git-repo-check \
  -m deepseek-flash \
  "只回复 OK"
```

该方式的优势是 OpenAI 或其他 provider 的根配置、默认模型与账号登录状态都不受影响；
每次需要 Hyprial 时显式加 `--profile hyprial` 即可。Codex Desktop 或不支持 profile
参数的入口若要默认选中 Hyprial，仍需使用下一节的根配置。

### 2.2 可选：写入 Codex 根配置

只有需要让所有 Codex 入口默认走 Hyprial 时，才把相同的根级字段和 provider section
合并进 `~/.codex/config.toml`：

```toml
model_provider = "hyprial"
model = "deepseek-flash"
model_reasoning_effort = "max"
disable_response_storage = true

[model_providers.hyprial]
name = "Hyprial API"
base_url = "https://api.hyprial.ai/v1"
env_key = "HYPRIAL_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
```

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'
codex
```

注意：本机若看到已有的 `experimental_bearer_token`，它会把明文 key 放在配置中。
共享机器或新配置应改用 `env_key`。Codex Desktop 与 CLI 通常读取同一份
`~/.codex/config.toml`，但图形端可能需要完全退出后重新打开才会读取新 provider。

### 2.3 与 codex-provider-sync 的同步关系

`codex-provider-sync` 只读取根 `~/.codex/config.toml`，不会解析
`~/.codex/hyprial.config.toml` 等 Codex profile 文件。因此两条路径要分开理解：

| 使用方式 | 日常 Codex 启动 | provider 会话同步 |
| --- | --- | --- |
| 独立 profile（推荐） | `codex --profile hyprial` | `codex-provider sync --provider hyprial` |
| 根配置 | `codex` | 根级 provider 已是 `hyprial` 时执行 `codex-provider sync` |

使用独立 profile 时：

- `codex-provider status` 显示的是根配置 provider，不会显示 profile 内的 `hyprial`。
- `codex-provider sync --provider hyprial` 可以只同步 rollout/SQLite 的 provider
  可见性，不修改根 `config.toml`。
- 不要为了让插件识别目标而直接执行 `codex-provider switch hyprial`；`switch` 会修改根级
  `model_provider` / `model`，正好破坏“不污染根配置”的目标。
- `sync --provider` 的模型元数据仍取自根级 `model`，因为插件不会读取 profile。它足以
  修复会话列表可见性，但不会把旧会话的 model 元数据替换成 profile 中的 Hyprial 模型。
  如果连模型元数据也必须完全一致，应使用根配置或独立的 `CODEX_HOME`。

推荐在关闭 Codex 后执行：

```bash
codex-provider status
codex-provider sync --provider hyprial
codex-provider status
```

根配置方式若准备第一次切换时交给插件，可先只合并 provider section，暂不修改已有根级
`model_provider` / `model`；`switch` 会同时改这两项并同步历史。

### 2.4 常见配置错误

- Base URL 应带 `/v1`，因为 Codex 会再请求 `/responses`。
- `wire_api` 必须是 `responses`，不要填 `chat`。
- 不要同时保留另一个在根级选中 `model_provider` 的配置；根级 provider 决定当前生效路由。
- 模型不存在时，先重新查询 `/v1/models`，再检查当前 key 的 group 是否有该模型。

## 3. Pi 接入

Pi 通过 `~/.pi/agent/models.json` 注册自定义 provider；`models-store.json` 是 Pi
自动维护的目录缓存，不要手工修改。团队方案是覆盖内置 `deepseek` provider 的 Base URL
和认证，并显式补充统一模型 ID `deepseek-flash`；Pi 自带的 `deepseek-v4-pro` 等其他
模型仍会保留：

```json
{
  "providers": {
    "deepseek": {
      "baseUrl": "https://api.hyprial.ai/v1",
      "apiKey": "$HYPRIAL_API_KEY",
      "authHeader": true,
      "models": [
        {
          "id": "deepseek-flash",
          "name": "DeepSeek Flash (Hyprial)",
          "reasoning": true,
          "input": ["text", "image"]
        }
      ]
    }
  }
}
```

使用：

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'

pi --provider deepseek --model deepseek-flash
pi --provider deepseek --model deepseek-v4-pro
pi --list-models
```

认证检查和最小验收：

```bash
pi auth check --provider deepseek --model deepseek-flash --json

pi --no-tools -p --provider deepseek --model deepseek-flash \
  "只回复 OK"
```

认证检查应返回 `{"status":"ready","provider":"deepseek","authType":"api_key"}`。
Pi 的原生 read tool call 也已通过 Hyprial 网关验证。该方式会让所有 Pi 内置
DeepSeek 请求改走 Hyprial；需要同时保留直连 DeepSeek 时，改用下一节的自定义
`hyprial` provider。

### 3.1 独立 hyprial provider

需要与直连 DeepSeek 并存时，注册单独的 provider：

```json
{
  "providers": {
    "hyprial": {
      "baseUrl": "https://api.hyprial.ai/v1",
      "api": "openai-completions",
      "apiKey": "$HYPRIAL_API_KEY",
      "authHeader": true,
      "compat": {
        "supportsDeveloperRole": false
      },
      "models": [
        {
          "id": "deepseek-flash",
          "name": "DeepSeek Flash (Hyprial)",
          "reasoning": true,
          "input": ["text", "image"]
        },
        {
          "id": "deepseek-v4-pro",
          "name": "DeepSeek V4 Pro (Hyprial)",
          "reasoning": true,
          "input": ["text"]
        }
      ]
    }
  }
}
```

```bash
pi --provider hyprial --model deepseek-flash
# provider/model 写法也可用
pi --model hyprial/deepseek-flash
```

`openai-completions` 与 `openai-responses` 均已验证可完成推理和工具调用，团队优先使用
兼容面更广的 `openai-completions`。`models.json` 中的模型列表是静态的，需要其他模型时
先查询 `/v1/models`，再把对应 `id` 加入列表。若希望每次启动自动读取网关目录，需要额外
编写 Pi extension 并在 `refreshModels({ signal })` 中调用 `/v1/models`。

内置 provider 覆盖会沿用 Pi 本地目录中的上下文与输出上限；Hyprial `/v1/models`
不提供这些容量字段，长上下文请求应结合网关实际模型能力选择保守值。

## 4. Claude Code 接入

Claude Code 使用 Anthropic Messages 协议。Base URL 只填网关根地址，SDK 会自行追加
`/v1/messages`：

```bash
export ANTHROPIC_BASE_URL='https://api.hyprial.ai'
export ANTHROPIC_AUTH_TOKEN="$HYPRIAL_API_KEY"
export ANTHROPIC_MODEL='deepseek-flash'
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1'
export CLAUDE_CODE_SUBAGENT_MODEL='deepseek-flash'
export CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP='1'

claude --model deepseek-flash
```

最小验收：

```bash
claude -p --model deepseek-flash "只回复 OK"
```

如果希望把非敏感设置持久化，可在本机 `~/.claude/settings.json` 中保存 Base URL、
默认模型与 subagent 模型，密钥仍通过环境变量或系统密钥管理器注入：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.hyprial.ai",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
    "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1"
  },
  "model": "deepseek-flash"
}
```

### 4.1 修复 subagent 继承模型时的报错

内置 Explore subagent 默认继承主进程模型，但 Claude Code 可能给这个继承加一层
`opus` 上限。主进程使用 Hyprial 模型时，这会让 Explore 尝试请求当前 key/group
没有提供的 `opus` 路由，表现为启动 Explore subagent 后报模型不存在、模型目录不匹配
或 400/404。

在 shell 或 `~/.claude/settings.json` 的 `env` 中增加：

```bash
export CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP='1'
```

```json
{
  "env": {
    "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1"
  }
}
```

该配置关闭内置 Explore agent 的继承上限，让它直接使用主进程当前模型。
`CLAUDE_CODE_SUBAGENT_MODEL` 继续作为其他 subagent 的默认模型，两者职责不同，
可以同时保留。修改后完全退出并重新启动 Claude Code，再触发一次 Explore/subagent
验证。

它的作用范围是内置 Explore agent 的模型继承，不是全局强制所有 subagent。自定义
agent definition 中显式声明的 `model:` 仍按其定义处理；这正好避免把普通 subagent
策略意外改成全部固定同一个模型。

如果团队当前确实必须把 `ANTHROPIC_AUTH_TOKEN` 放在 `settings.json`，该文件只能保留在
本机并设为 `0600`，绝不能提交、截图或转发。密钥支持替换，发现泄露后应立即轮换。

常见错误：

- Claude Code 请求变成 `/v1/v1/messages`：Base URL 多写了 `/v1`。
- 401/403：key 错误、被禁用，或当前 group 没有可用模型。
- 模型名不符：先查询 `/v1/models`，不要复制其他成员的模型清单。
- Explore/subagent 启动即报模型不支持：确认 `~/.claude/settings.json` 的 `env` 中有
  `"CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1"`，然后完全重启 Claude Code。

## 5. 用 codex-provider-sync 修复历史会话

### 5.1 它解决什么问题

Codex 切换根级 `model_provider` 后，历史消息通常还在磁盘中，但 rollout 文件和 Codex
SQLite 索引仍记录旧 provider。结果是 Codex Desktop 或 `/resume` 看不到这些会话。

`@dailin521/codex-provider-sync` 只同步会话的 provider/model 可见性元数据，包括：

- `~/.codex/config.toml` 中根级 `model_provider` 和 `model`
- `sessions/**/rollout-*.jsonl` 与 `archived_sessions/**/rollout-*.jsonl`
- Codex SQLite 中的线程 provider/model 索引
- Codex Desktop 的工作区/项目路径状态

它不会登录账号、切换凭据、修改 `auth.json`，也不会重写消息正文。

### 5.2 安装与检查

```bash
npm install -g @dailin521/codex-provider-sync
codex-provider --help
codex-provider status
```

执行同步或恢复前，先退出 Codex CLI、Codex Desktop 和正在运行的相关 app-server。
SQLite 被占用时，工具会在修改 rollout 前停止。

### 5.3 推荐操作顺序

**使用独立 profile 时**，日常用 `codex --profile hyprial`，关闭 Codex 后同步历史：

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'

codex-provider status
codex-provider sync --provider hyprial
codex-provider status
```

该命令只更新 rollout/SQLite 的 provider 可见性，不修改根 `config.toml`。注意
`status` 仍显示根配置 provider，不会识别 profile 内的 `hyprial`；同步身份由显式
`--provider hyprial` 指定。模型元数据限制见 2.3 节。

**使用根配置时**，若还没有把根级 `model_provider` 切到 `hyprial`，可以只合并 provider
section，再交给插件完成切换和同步：

```bash
export HYPRIAL_API_KEY='sk-REPLACE_ME'

codex-provider status
codex-provider switch hyprial --model deepseek-flash
codex-provider status
```

`switch` 会同时修改根级 provider/model 并同步历史元数据。如果已经手工把根级配置切到
`hyprial`，只需执行：

```bash
codex-provider sync
```

需要在脚本中指定目标 provider 时：

```bash
codex-provider sync --provider hyprial
```

完成后重新打开 Codex，检查历史会话与 `/resume`。想切回原生 OpenAI 路由时：

```bash
codex-provider switch openai
```

`codex-provider` 读取的是 `~/.codex/config.toml` 根级配置。不要把 provider 只放进
`~/.codex/hyprial.config.toml` 等 profile 文件后，就期待 `status` / `switch` 或
Web UI 自动识别它。profile 路径应显式使用 `codex-provider sync --provider hyprial`；
只有希望根配置本身切到 Hyprial 时才使用 `switch`。

### 5.4 使用本地 Web UI

不熟悉 CLI 的成员可以使用同一插件的浏览器界面：

```bash
codex-provider web
```

默认只监听 `127.0.0.1:8791` 并打开配对页面。不要把它绑定或暴露到公网、局域网。远程
机器通过 SSH 回环转发使用：

```bash
# 本地终端
ssh -L 8791:127.0.0.1:8791 user@server

# 远程终端
codex-provider web --no-open
```

配对链接是一次性的；需要重新配对时使用：

```bash
codex-provider web --reset-access
```

### 5.5 备份、恢复与边界

- 每次 `sync` / `switch` 前，工具会在
  `~/.codex/backups_state/provider-sync/<timestamp>` 创建备份，默认保留最近 5 份。
- 备份可能包含完整会话和 SQLite 内容，只保存在本机，不要上传群聊或共享盘。
- 回滚一次同步：

```bash
codex-provider restore /path/to/.codex/backups_state/provider-sync/<timestamp>
```

- 输出提到 `Skipped locked rollout files` 时属于部分成功；结束活动会话后再次执行
  `codex-provider sync` 即可补齐。
- 旧会话含其他 provider 的 `encrypted_content` 时，同步只能恢复列表可见性，不能保证
  旧会话可继续或压缩。需要可靠续写时，回到原 provider/账号，或新建会话。
- provider-sync 不是认证工具。Codex 的 401/403 必须在 Codex provider/密钥层解决。

## 6. 最短操作清单

```bash
# 1. 当前 shell 注入网关密钥
export HYPRIAL_API_KEY='sk-REPLACE_ME'

# 2. 确认当前密钥可见的模型
curl -sS https://api.hyprial.ai/v1/models \
  -H "Authorization: Bearer $HYPRIAL_API_KEY"

# 3. 配置 ~/.codex/hyprial.config.toml 后，日常用独立 profile 启动
#    日常入口：codex --profile hyprial
#    关闭 Codex 后，同步历史时显式指定 provider，不修改根 config.toml
npm install -g @dailin521/codex-provider-sync
codex-provider sync --provider hyprial

# 4. Pi：按第 3 节写入 ~/.pi/agent/models.json 后启动
pi --provider deepseek --model deepseek-flash

# 5. Claude Code 使用同一把 key
export ANTHROPIC_BASE_URL='https://api.hyprial.ai'
export ANTHROPIC_AUTH_TOKEN="$HYPRIAL_API_KEY"
claude --model deepseek-flash
```

## 7. 安全要求

- 每位成员使用自己的网关 key，不共享、不把 key 写入仓库、文档、截图或聊天记录。
- Codex 配置优先使用 `env_key`；Claude Code 使用环境变量或系统密钥管理器。
- Pi `models.json` 使用 `apiKey: "$HYPRIAL_API_KEY"` 引用环境变量，不写明文 key。
- `codex-provider web` 只允许回环访问，不要在服务器上监听 `0.0.0.0`。
- provider-sync 备份和 Codex rollout 都可能包含业务代码、提示词和对话内容，按敏感数据处理。

参考：

- [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
- [Pi custom models and providers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)
- [codex-provider-sync](https://github.com/Dailin521/codex-provider-sync)
