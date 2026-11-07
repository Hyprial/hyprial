
## 12. 2026-09-27 默认插件与运行组件随载荷更新

用户授权：默认插件、提供方和运行组件版本应通过载荷更新，不要求逐项发布新壳。
新增可选 manifest entries：`profilePolicy`（file）、`componentPolicy`（dir）。均受原有
entryDigests、目录边界、归档校验与 generation 切换约束；旧载荷缺失时保持兼容基线。

- profilePolicy schemaVersion=1：baseBundles、plugins（name、entry、可选 subpath、
  dependenciesEntry、bundle）、providers。entry 引用父 manifest 的已解析条目；不得出现
  外部路径、越界子路径或安装任意网络脚本。版本由已校验包体的 package.json 提供。
- 新清单只接管声明的产品项。用户额外 dependencies/bundles、patch 和其他字段保留；
  产品项被用户移出启用清单后记住禁用。移除旧产品项仅清理仍等于上次管理值的引用。
  回滚采用相同调和规则，不能删除用户密钥、模型或自定义 provider。
- providers 是默认配置，不是凭证。仅补缺失路由，不替换已有路由；不联网调用模型。
- componentPolicy 目录含 catalog.json 及各组件 package.json/package-lock.json。
  package namespace、执行入口和组件种类仍由壳限制；版本、下载 hash、依赖锁随载荷。
  用户外部工具不被原地更新；产品下载的组件保留多版本并重新校验后选择。
- 更换清单或依赖必须通过完整载荷校验与启动验收；初始新壳须仍带可离线启动的默认插件。
- 壳更新使用独立可信发布通道与平台安装/签名机制，不能把 app.asar 当载荷覆盖。
  统一菜单按壳和载荷检查结果展示；未配置可用壳发布源时如实显示未配置，不声称已最新。

## 实现与发布

`desktop/defaults/profile.json` 是默认插件和 provider 的发布输入。构建把它与运行组件
catalog/lockfiles 放入 `update-policy/`，登记 manifest entries 并计算各自 digest。
改插件列表或版本时只需发布新的 payload generation；客户端的通用物化器不需要随之改动。
产品管理的插件替换有恢复日志；失败恢复原插件和配置。用户自己的依赖、补丁和显式禁用
保留。provider 仅补缺失项，不重置已有配置，也不把密钥写入载荷。

`componentPolicy` 管理受支持的 node/dsh/codex/claude 版本和锁文件。已由客户端下载的
组件会在下一次启动时准备匹配新载荷的版本；外部工具仍由用户选择。失败恢复原选择，
保留已下载目录。组件版本回滚依据上一代 policy 同样执行。

外壳更新采用 electron-updater 6，包内 `src/update-channel.json` 是可信通道配置。
统一的「检查更新」先报告客户端更新，再检查载荷。`autoUpgrade` 开启时后台下载验证
客户端更新；安装仍由用户确认重启，以免终止活动会话。载荷保持原有下次启动生效策略。
壳更新无法使用现有载荷回滚菜单回退，菜单文案明确限定为「载荷」。

内部测试基线继续 `shell: null`，未配置显示未配置；不伪造公共地址或签名身份。
启用壳更新需要填写 shell.url（HTTPS 目录）、publisherName（Windows 发布者）、
macIdentity（Developer ID），并在平台构建环境准备签名与公证。build-release 自动配置
generic publish 元数据、Windows 签名、macOS dmg+zip 与公证；仍然 `--publish never`，
不自动上传。按平台验收后先发布安装文件及 blockmap，最后发布 latest.yml/latest-mac.yml。
参考：https://www.electron.build/v26/docs/features/auto-update/

必须先发布一次包含通用 policy 加载器的引导客户端，再在该客户端的更新通道发布新增插件
载荷。旧客户端的发布通道保留兼容载荷，不能把添加 entries 当作旧客户端理解新协议的证明。
现有 Linux 单元测试不能代替 Windows/macOS 安装、签名、重启和真实跨版本升级验收。

引导客户端版本为 0.1.3。壳更新按应用 semver 判断新旧；后续修改壳必须递增
`desktop/package.json` 的 version，不能只增加 payload 的 r 修订号。载荷独立使用
原有 `<appVersion>-r<number>`。两个版本维度在更新界面分别报告。
