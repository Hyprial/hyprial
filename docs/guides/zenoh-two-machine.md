# 双机部署:Zenoh 显式端点配置

> ⚠️ **2026-08-28 更新:日常入网【不再需要】照本文手工填端点。**
> daemon 现在默认会向本机 tailscale 要 tailnet 的 peer 表并自己试连
> (`HYPRIAL_PEER_DISCOVERY=0` 可关)。**新成员入网请看
> `onboarding-checklist.md`,不要照本文填地址。**
>
> 本文继续有效的场景是**显式端点本身**:没有 tailscale 的环境、
> 隔离测试、以及需要精确控制"只连这一台"的时候 —— 显式配置的端点
> 永远优先且不会被发现覆盖。
>
> ⚠️ 而下一节那句「`to_json` **固定输出** scouting 全 false」已经过时:
> 那两个开关现在是 `ZenohConfig` 的字段(默认仍然是关)。**默认值没变,
> 变的是它不再写死** —— 而 gossip 我们刻意不用,理由见
> `src/hyprial/daemon/discovery.py` 的模块注释。

> 本文档说明如何把两台机器上的 daemon 通过 Zenoh 显式端点连成一个 mesh。
> 它是 E2E-001(两机 send/reply/ack)的部署前置,也是 TypeScript→Python
> cutover 前最后一扇门。

## 为什么必须显式配置端点

`hyprial` 的 Zenoh 传输层默认**关闭 multicast 与 gossip 自动发现**
(`ZenohConfig.to_json` 固定输出 `scouting.multicast.enabled=false`、
`scouting.gossip.enabled=false`)。这是隔离测试与生产安全的选择:daemon
之间只按你写明的端点互相连接,不会因为在同一子网广播而互相串扰。

因此两台机器要互相发现,必须满足:

- 至少一台 `listen`(监听一个端口);
- 另一台(或双方)`connect` 到对方的监听地址;
- 两端使用同一个 `HYPRIAL_NODE_ID` 命名空间下的不同 node id;
- 单机双 HYPRIAL_HOME 的 loopback 测试默认能通,但**真双机必须写端点**。

## 配置入口(按优先级)

1. **环境变量**(每次启动的临时覆盖,优先级最高)
   - `HYPRIAL_ZENOH_LISTEN=tcp/<ip>:<port>[,tcp/<ip>:<port>...]`
   - `HYPRIAL_ZENOH_CONNECT=tcp/<ip>:<port>[,tcp/<ip>:<port>...]`
2. **desired state**(持久化,重启保持)
   - `hyprial init --listen <endpoint> --connect <endpoint>` 会写入
     `$HARNESS_STATE_DIR/desired-state.json.v1` 的 `zenoh` 段;之后即使
     不带任何参数重启 daemon,端点依然生效。
   - 传空字符串可清空单侧:`hyprial init --listen "" --connect ""`。
   - daemon 正在运行时改端点会被拒绝(先 `hyprial daemon stop`)。
3. 什么都不配:peer 模式 + loopback,仅本机单实例。

合并规则(daemon 启动时):环境变量有值则用它,否则回退 desired state;
listen 与 connect 各自独立。

`hyprial daemon status --json` 的 `zenoh` 字段会报告当前生效的 listen/connect,可用来核对。

## 半开检测与自动重连

每个 daemon session 默认显式启用 Zenoh 1.9.0 的 link lease 与 connect
retry。默认值如下:

| 参数 | 默认值 | 临时环境覆盖 |
|---|---:|---|
| link lease | `6000ms` | `HYPRIAL_ZENOH_LEASE_MS` |
| 每个 lease 的 keepalive 数 | `4` | `HYPRIAL_ZENOH_KEEP_ALIVE` |
| 重拨初始等待 | `500ms` | `HYPRIAL_ZENOH_CONNECT_RETRY_INITIAL_MS` |
| 重拨最大等待 | `8000ms` | `HYPRIAL_ZENOH_CONNECT_RETRY_MAX_MS` |
| 重拨等待倍数 | `2.0` | `HYPRIAL_ZENOH_CONNECT_RETRY_MULTIPLIER` |

`keep_alive` 是一个 lease 周期内发送的次数,不是毫秒数;默认空闲链路约每
`1.5s` 发送一个 Zenoh keepalive。接收端在对方通告的 lease 内没有收到任何
frame 时关闭该 link。显式 connect endpoint 的重拨序列为
`0.5s, 1s, 2s, 4s, 8s, 8s...`,且 `exit_on_failure=false`,因此对端恢复后
由 Zenoh 原 session 自动重连并重发 liveliness/queryable/subscriber 等声明。

这些值也可以在代码中通过 `ZenohConfig` 的同名字段覆盖。非法或不安全的值
(非正 lease/keepalive/初始等待、最大等待小于初始等待、倍数小于 1 或非有限
数)会使 daemon 启动响亮失败,不会静默退回默认值。

隔离回归命令(动态端口,不使用 `7447`,两个独立 HYPRIAL home):

```sh
HYPRIAL_BIN="$(pwd)/.venv/bin/hyprial" \
  .venv/bin/python contract/e2e/run-zenoh-half-open-reconnect.py
```

runner 在两 daemon 之间放置一个用户态 TCP 黑洞代理:listen 端 daemon 被
`SIGKILL` 后,代理继续读取 connect 端数据,让该侧内核 TCP 保持建立但不返回
Zenoh frame。测试必须在阈值内看到远端 target 消失,重启后双方 target 恢复,
再完成一次消息投递与 ack;代理使用 `selectors`,不受高 FD 场景下
`select()` 的描述符上限影响。

## 真机部署(本组织的 Tailnet)

| 机器 | Tailnet IP | 建议角色 |
|---|---|---|
| mac-studio | `100.64.30.3` | 常驻监听者(listen) |
| MacBook | `100.64.30.4` | 连接者 + 自己的监听(connect + listen) |

端口建议:**`7447`**(任意未占用高位端口均可;两台机器各监听一个端口)。
两侧都 listen、两侧都 connect 是最稳的 peer mesh 形态——任一方先启动都能
互连,单条 TCP 断了也会由另一条补上。

### mac-studio(监听 `7447`,连接 MacBook `7447`)

```bash
hyprial init \
  --listen tcp/100.64.30.3:7447 \
  --connect tcp/100.64.30.4:7447 \
  --json
```

### MacBook(监听 `7448`,连接 mac-studio `7447`)

```bash
hyprial init \
  --listen tcp/100.64.30.4:7448 \
  --connect tcp/100.64.30.3:7447 \
  --json
```

> 两台各自监听不同端口、彼此互连:mac-studio 的 connect 打到 MacBook 的
> listen,MachBook 的 connect 打到 mac-studio 的 listen。不想让其中一台
> 被入站连接时,可以只让被连方 listen、连入方只 `connect`(见下方最小形态)。

最小形态(mac-studio 只 listen,MacBook 只 connect):

```bash
# mac-studio
hyprial init --listen tcp/0.0.0.0:7447 --json
# MacBook
hyprial init --connect tcp/100.64.30.3:7447 --json
```

等价的环境变量写法(适合 launchd/systemd service 注入):

```bash
# mac-studio
HYPRIAL_ZENOH_LISTEN=tcp/100.64.30.3:7447 \
HYPRIAL_ZENOH_CONNECT=tcp/100.64.30.4:7447 \
hyprial init --json

# MacBook
HYPRIAL_ZENOH_LISTEN=tcp/100.64.30.4:7448 \
HYPRIAL_ZENOH_CONNECT=tcp/100.64.30.3:7447 \
hyprial init --json
```

### 防火墙与网络注意点

- **macOS 应用防火墙**:首次 listen 时允许 `hyprial`(或 Python)接受传入连接;
  若之前选了"拒绝",这是一种静默故障——mesh 无法建立,但 `hyprial doctor` 仍
  全绿(doctor 只校验配置,不校验可达性)。在 系统设置 → 网络 → 防火墙 → 选项
  中删除该条目重置授权,改为允许,并重启 daemon。
- **Tailnet 内网**:Tailscale 默认放行 100.x.y.z 网段,无需开公网端口;
  不要在 Tailnet 之外暴露 7447/7448(zenoh 明文 TCP)。
- **不要依赖 multicast/gossip**:scouting 已关闭;两端必须能直接 TCP 到
  对方监听地址(同一个 Tailnet 或同一局域网)。企业 WiFi 的客户端隔离
  (AP isolation)会阻断,请走 Tailnet。
- **端口不冲突**:监听端口在本机必须是空闲的;`lsof -iTCP:7447 -sTCP:LISTEN`
  可检查。
- 连不上的快速排错:`hyprial daemon status --json` 看 `zenoh.listen/connect` 是否生效;
  `nc -vz <对端IP> <端口>` 验证 TCP 可达;检查 `daemon-launch.log` 里的
  zenoh 报错。

## 验证

```bash
# 两台机器上各自
hyprial daemon status --json          # zenoh 段应为已配置端点

# 从 mac-studio 发起,确认能看到 MacBook 的 node id
hyprial message targets --json

# 两机往返
hyprial message send --from <local-actor> --to <remote-actor> hello --json
```

mac-studio 上出现 MacBook 的 node id,即 mesh 已建立;随后可按
`contract/e2e/01-two-machine-roundtrip.md` 执行完整
send/reply/ack 契约。

## 本地模拟(无第二台机器)

两条自动路径,一条命令随时复跑:

### Docker 双容器(推荐,真跨网络语义)

```sh
uv run python contract/e2e/run-two-machine-roundtrip.py
```

runner 会构建 `hyprial-e2e001:local` 镜像(首次拉 `python:3.12-slim`,后续复用),
在独立 bridge 网络 `hyprial-e2e001-net` 上起 `hyprial-e2e001-a`(listen `:7447`)/
`hyprial-e2e001-b`(listen `:7448`),两个容器各有独立网络栈与 IP,TCP 跨容器与
跨真机同语义。覆盖:

1. 显式端点生效 + 双向 send/reply/ack;
2. **离线排队**:`docker stop hyprial-e2e001-b` → A 发消息(入 A 的 durable
   outbox)→ `docker start hyprial-e2e001-b` → 消息送达并回执,A 的 outbox 排空;
3. **重启自愈**:`docker stop/start hyprial-e2e001-a` 后 mesh 自动重建,再次
   roundtrip 通过。

### 容器清理是硬保证,不是尽力而为

- 所有资源用固定前缀 `hyprial-e2e001-*`(容器/网络/镜像标签);镜像用固定 tag
  `hyprial-e2e001:local` 复用,构建后 prune 掉上一版 dangling 层,不堆积镜像。
- **启动前先预清**:脚本开头删除同前缀残留(上一次异常退出留下的容器/网络),再建新资源。
- **结束必清**:整个运行包在 `try/finally` + SIGINT/SIGTERM 信号处理里;无论
  成功/失败/异常/Ctrl-C,都会先导出证据(仅文件),再删容器与网络,最后跑
  **收尾自检**——`docker ps -a --filter name=hyprial-e2e001` 与
  `docker network ls --filter name=hyprial-e2e001` 断言为空,有残留立即报 FAIL。
- 失败/中断时容器不留,**日志保留**在 `/tmp/hyprial-e2e001-failure-<ts>/`
  (docker logs、`daemon.jsonl`、`desired-state.json.v1`)。
- 万一仍有残留,手动兜底:

```sh
docker rm -f $(docker ps -aq --filter name=hyprial-e2e001) 2>/dev/null
docker network rm hyprial-e2e001-net 2>/dev/null
# 若本脚本没有内置容器网络(compose)或卷,则无需额外清理;
# 脚本未使用 compose,也未创建命名卷。
docker image prune -f --filter label=hyprial-e2e001=1
```

无 Docker 的环境用 `--local` 回退到单机双 HYPRIAL_HOME + 双 TCP loopback。

### 容器内如何持久化端点

容器 entrypoint(`contract/e2e/docker/entrypoint.sh`)每次启动把
`HYPRIAL_ZENOH_LISTEN`/`HYPRIAL_ZENOH_CONNECT` 写入 desired state(与
`hyprial init --listen/--connect` 相同的 `DesiredStateStore` 路径),然后以
**无 zenoh 环境变量**的方式 `exec hyprial daemon run`(daemon 即 PID 1,优雅
收 SIGTERM)。daemon 每次启动都从 desired state 恢复端点——这正是真机
重启必须满足的持久化契约。

### 真机验证

容器测试通过后,把端点换成两台 Tailnet 机器的地址,按上方命令手动跑
`hyprial init --listen/--connect`;`[REAL ENV]` 步骤由协调者与 Allen 执行。
