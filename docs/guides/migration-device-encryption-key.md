# 迁移说明：目录设备行携带设备加密钥公钥

适用于：把一个 org 内的 hyprial 设备升级到「daemon 在自己的目录设备行上发布加密钥公钥」的版本的管理员，或代为操作的 agent。

一句话：**没有迁移代码，也没有迁移命令**。升级后的 daemon 在下一次发布自己的设备行时，在行上多带一对可选键 `encryptionKeyId` / `encryptionPublicKey`；**同一 org 内的设备需一并升级**，原因是旧版本的 daemon 读到带这两个键的行会把这一行判为无效（见第 2 节）。

## 1. 这次改动做了什么

- 目录设备行（`directory/devices/<owner>/<deviceId>.json`）新增一对**成对的可选**键：
  - `encryptionPublicKey`：本设备 X25519 公钥的 32 个原始字节，标准 base64；
  - `encryptionKeyId`：`x25519:` 加上这 32 个字节的 SHA-256 摘要的前 32 个小写十六进制字符。
- 两个键要么都有，要么都没有。行里出现其中一个而没有另一个、键的格式不对、`encryptionKeyId` 不是 `encryptionPublicKey` 的摘要、或公钥是低阶点，整行无效。
- 行的键集仍是封闭的：出现任何别的键（例如 `encryptionKeyPrivate`）整行无效。私钥从不写入目录。
- 加密钥是 `kind="user"` 的 X25519 钥，由本机的加密钥 store 保管（`<state>/identity/encryption-keys/<slug>/key.json`，`<state>` 是 daemon 的状态目录，默认为 `HYPRIAL_HOME` 下的 `state`）。私钥不出本机。

没有数据需要转换：旧版本的行（不带这两个键）在新版本下照常有效。

## 2. 为什么必须一并升级

旧版本的目录读取代码对未知的键是拒绝的。这一点在一个真实的 OrgFS 存储上实测过：先由升级后的版本往同一个 org 写入两台设备的行（一台带这两个键，一台不带），再分别用两个版本的代码读取同一个存储目录。

- **旧版本读取**：只保留不带键的那一行；带键的那一行被丢掉，日志记 `directory device record has unknown keys: ['encryptionKeyId', 'encryptionPublicKey']`，`org.network` 里把它报成一条 `invalid-device-record` 数据问题（指向那台设备）。
- **新版本读取**：两行都保留，没有数据问题。

这次实测没有覆盖的范围：两行是由目录存储接口写入的（不是 daemon 的 `publish_self`），也不是两个版本的 daemon 进程在网络上互通。结论不依赖这两点。

因此：org 内一旦有一台设备升级并重新发布了自己的行，未升级的设备就在目录视图里看不到它，直到它们也升级。反向没有问题——升级后的设备读不带这两个键的旧行照常有效。

与 `deviceUid` 不同，这一对键**没有开关**可以推迟：升级后的 daemon 只要具备第 3 节的前提，就会在下一次 `publish_self` 时带上它们。`publish_self` 由 daemon 的周期性转发维护触发，并且只在 sidecar 已写入地址文件之后执行：第一次（或地址变化时）立即发布，否则至多每 10 秒一次。因此升级顺序只能是「先停、全部升级、再启动」，不能让新旧版本在同一 org 内同时在线发布。

## 3. 哪些设备会带上这一对键

一台设备带键，需要同时满足：

1. `settings.json` 里有已提交的账号：`owner`、`identityIssuer`、`userId`；
2. 设备记录里有 `deviceUid`（`state/tailcat/device.json`）；
3. 设备记录的 `owner` 与 `settings.json` 的 `owner` 一致；
4. 本机状态目录可以保管私钥（目录属主是当前用户、权限为仅本人可访问；不支持这种访问控制的平台上不带键）。

不满足的设备不生成钥，发布的行不带这两个键，也不报错。具体是：

- 没有 `userId` 的 home（未登录，或本地使用模式）；
- 设备记录早于 `deviceUid` 的引入、没有 uid 的设备；
- 设备记录属于另一个账号的 home；
- 加密钥记录因机器指纹不符而被隔离（把 home 从另一台机器拷贝过来会发生这种情况），或状态目录权限不安全的设备；
- 不支持私有文件访问控制的平台。

这些设备在修好对应的前提之后，会在下一次发布时自动带上键，不需要额外的命令。被隔离的钥不会被自动替换。

## 4. 升级步骤

> [!IMPORTANT]
> 「离线」指先执行 `hyprial daemon stop`，再用 `hyprial daemon status` 确认没有运行中的 daemon 之后，再检查或移动文件。不要在 daemon 运行期间修改 `settings.json` 或 `state/` 下的文件。

1. 盘点 org 内的全部设备，确认每一台都能在同一个维护窗口内升级。做不到的话先不要升级任何一台（见第 2 节）。

2. 在每台设备上停掉 daemon：

   ```sh
   hyprial daemon stop
   hyprial daemon status
   ```

3. 对每台设备做只读备份（可选，但建议）：整个 `HYPRIAL_HOME` 目录。本次升级不改写任何既有文件，备份只是为了能回退。

4. 在每台设备上安装新版本。生成加密钥和发布带键的行都只发生在 daemon 运行期间，安装过程本身两者都不做。

5. 启动 daemon：

   ```sh
   hyprial daemon start
   ```

   daemon 进入 serving 后为具备第 3 节前提的设备生成加密钥（已有一把时沿用那一把），并在自己加入了 org 且 sidecar 已写入地址文件之后，在下一次发布设备行时带上公钥。生成钥的入口是 serving 启动与重新登录时的恢复流程，以及内存缓存未命中时的首次目录发布；稳态下的周期性发布复用按 `(userId, deviceUid)` 缓存在内存里的公钥对，不再重复进入钥存储。两个入口读的是同一个本机钥存储，得到的是同一把钥。

## 5. 升级后验证

在每台设备上：

1. 确认只有一把钥（把 `<HYPRIAL_HOME>` 换成实际路径；daemon 单独配置了状态目录时，把路径里的 `state` 换成那个目录）：

   ```sh
   ls <HYPRIAL_HOME>/state/identity/encryption-keys/*/key.json
   ```

   预期恰好一行输出。不带键的设备（第 3 节）此时这个目录不存在，也是预期结果。

2. 确认目录行带上了键。把 `<org>` 换成 org 名：

   ```sh
   hyprial org network <org> --json
   ```

   CLI `--json` 输出单个 JSON 对象，顶层带 `"ok": true`、`orgs`、`directory`、`directoryPending`、`self` 和 `dataProblems`。设备行列表位于 `directory["<orgKey>"]` 数组中（`<orgKey>` 为 `orgs[0]`，即带 `group-` 前缀的规范 org 名，例如查询 `acme` 或 `group-acme` 时对应键均为 `"group-acme"`），而不是直接平铺在 `directory` 上。在该数组中按 `deviceId`（与 `self.deviceId` 相同）找到本机那一行，确认同时有 `encryptionKeyId` 和 `encryptionPublicKey`，且 `encryptionKeyId` 的值以 `x25519:` 开头。顶层 `dataProblems`（以及 `directoryPending["<orgKey>"]["dataProblems"]`）里不应出现 `reason` 为 `invalid-device-record` 且指向这台设备的条目（其它原因的数据问题与本次升级无关）。

3. 在 org 内另一台已升级的设备上重复第 2 步，确认它在 `directory["<orgKey>"]` 里能同时看到自己和第一台设备的行，且 `dataProblems` 里没有指向这两台设备的 `invalid-device-record` 条目。

## 6. 回退

刚回退到旧版本并启动、尚未执行 `publish_self` 时，目录里已经发布的行仍带着 `encryptionKeyId` 和 `encryptionPublicKey`，旧版本 daemon 读取时会把这一行判为 `invalid-device-record` 并从目录视图里丢掉（第 2 节）。但一旦 sidecar 写入地址文件，旧版本 daemon 在下一次 `publish_self`（启动后立即或 10 秒维护周期内）会构造一条不带这两个键的旧格式行并直接覆写自己在 OrgFS 里的设备行；覆写完成后，旧版本 daemon 随即重新看到自己的设备行。

因此：

- **整台 org 全部回退并启动**：各设备在各自下一次 `publish_self` 用不带键的行覆写自己的目录行后，都会恢复可见。
- **部分回退（新旧版本混跑）或曾升级的设备离线未启动旧版**：那些仍带着 `encryptionKeyId` / `encryptionPublicKey` 的设备行，会被旧版本 daemon 持续判为 `invalid-device-record` 丢弃。

本机的加密钥文件不会被回退删除；日后重新升级后会沿用它，不会生成第二把。

## 7. 已知限制

- 登出不从目录里撤下这一对键。登出后 daemon 不再给出钥（下一次构建的服务读不到钥），但它也没有写目录的权限，刷新被拒绝，它留在 OrgFS 里的行原样保留——包括 `encryptionKeyId` 和 `encryptionPublicKey`——以及行上原有的其它字段。留在目录里的是公钥，不是私钥。登出也不删除本机的加密钥文件（签名钥的行为与此相同）。是否需要在登出时撤下目录行，不属于本次改动。
- 一台设备只有一个 `userId` 时才带键。同一台机器上切换到另一个账号（换 `HYPRIAL_HOME`）会为那个账号生成另一把钥，与原来的钥互不相干。
