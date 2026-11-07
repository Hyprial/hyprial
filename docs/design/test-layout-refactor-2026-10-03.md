# Python 测试目录收拢（2026-10-03）

## 目标与边界

本轮只调整 `tests/` 的组织、测试间引用与 fixture 定位，不改变产品行为，不删除、降低或跳过用例。
Python 五个深模块已经落地，测试根目录仍有 738 个 Python 文件，测试全树有 50 个超过 1000 行的文件。
先让测试所有权和运行方式明确，再逐主题拆大测试文件；本轮不把目录迁移冒充产品全套通过。

产品代码、根依赖文件、CI、contract 和 scripts 由主集成模型单点写入。
`tests/integration/desktop/test_tauri_handoff.py` 是主模型并行新增文件，不迁移、不编辑；
移前与移后 collect 命令都显式排除它，避免把并行新增当作收拢遗漏。

## 路径政策

- `kernel/`：基础契约、actor 运行内核、地址语法、日志/HookBus、process 等平台原语。
- `identity/`：agent 配置/家目录/凭据、provider 身份与认证、所有权与能力授权。
- `daemon/`：durable 收发、生命周期、会话、harness/adapter、传输、OrgFS、PAC 与应用装配。
  PAC graph/actor/dispatch/journal/workflow runtime 的权威实现是 `daemon/impl/pac`，
  其 unit/authority 测试也属于本域，不能仅因业务名称包含“工作流”便归到 biz。
- `biz/`：routine、usage/quota 等上层业务规则。组织、squire、dispatch、durable user delivery
  的权威实现当前属于 daemon，不因业务名称而挪入 biz；canonical 用户与 profile store 属 identity。
- `shell/`：CLI、GUI 安装生命周期、登录/首次设置、安装/升级、用户入口。
- `integration/`：跨域 transfer、隔离 E2E runner 语义、native harness、desktop、SmolVM。
- `tooling/`：CI/发布策略、结构门、契约工具、迁移工具与测试选择器。
- `support/`：真正复用的测试构造器和 gate 实现，按 runtime/release/PAC/CI 等职责分组。
- `fixtures/`：静态和自执行假服务，按 network/harnesses/Lark/GUI/home/SmolVM 等主题收拢。

分组依据是被验证的职责/契约，而不是文件名前缀或 import 数量投票。纯 CLI subprocess/argv
契约归 shell，即使验证输出时导入 daemon store；routine removal 场景仍归 biz，即使用 daemon
装配。跨模块拼装、真实外壳/多 home 场景归 integration；扫描源码、反向 mutation 与注册表
冻结门归 tooling，而不假装是某个运行时域的 unit test。

每个主题目录最多 6 个测试/辅助文件，再加 1 个包标记。超过这个数量的旧大主题按可辨识的
子主题词继续分层，不按数字编号分桶。文件名保留，具体逐文件映射是证据目录中的 manifest。
三个现有 authority JSON 原路径及字节保持不变，避免把测试目录整理混入豁免政策变更。
测试根仅保留 suite-wide `conftest.py`、包标记、三个 authority JSON 与目录说明。

主审后作一次 PAC owner 修正：初轮 48 个 PAC 测试中，45 个归 `daemon/pac`，纯 principal
语法测试归 `identity/principals`，CLI 退役面测试归 `shell/cli/workflows`，CLI-only 黑盒 E2E
归 `integration/pac/events`。原 v1 manifest/证据不改写；`pac-owner-amendment.json` 记录
48 条链式映射。之后人工逐职责复核，一次完成 95 条 topic owner 修正：53 条主审批准项，
42 条“其余明确错误一次收拢”的补充项；另 3 条合理 subject 分组保留并写明理由。
`topic-owner-amendment.json` 记录原输入、输出 SHA/mode、职责解释和 canonical consumer 改写；
初轮、PAC 与 53 条 draft 证据均保留，`final-path-map.json` 是原始路径到当前路径的合成权威表。

## 导入与 fixture 不变量

使用 `tests.<domain>.<topic>.<module>` 的 canonical package import，禁止数百个旧路径 facade/shim。
目录包标记让 pytest 与直接测试间导入使用同一模块身份；不再依赖扁平目录被塞入 `sys.path`。
根 conftest 的 fixture 范围、隔离 home/network 与 FD leak gate 都原样保留。
真实 `Path(__file__)` 祖先与 sibling fixture 引用重定向到仓库锚点；不改动态生成程序中独立的
`__file__` 语义。跨测试共享 fixture 的绑定别名、monkeypatch 模块身份与 pytest 参数化名称保持。

## 可逆迁移与验收

迁移前采集全部 node ID，而不是只看数量。现有根依赖环境通过任务私有 uv overlay 补入
`libcst==1.9.0`（迁移工具测试需要），不修改产品锁文件或共享环境。
有效基线为 **9988 个 node ID、0 个 collection error**；隔离 conftest 的 loopback 预热需要
本机执行授权，生产 daemon、凭据和用户配置始终不进入测试。

完整路径映射逐项钉定原文件 SHA-256、mode 与改写后 SHA-256；写入前验证整个 preimage 集，
保存可恢复副本，然后才 bulk rename。移后必须逐 ID 按路径映射对账，不接受数量相同但用例换掉。
验证顺序为 collect 对账、迁移工具测试、跨域及 fixture 焦点测试、Ruff 和结构扫描。
已存在的产品失败必须与新路径/collector 失败区分，不下调断言或测试期望凑绿。

## 大文件收拢阶段

用户随后明确要求本轮完成 50 个 >1000 行测试/辅助文件的收拢。主模型冻结 95-owner checkpoint
与产品树后，将实施范围分给三个互斥 worker：daemon 30 个文件、其余域 19 个文件、CI support
1 个文件。该阶段的逐职责映射另起 `docs/evidence/test-long-files-2026-10-03/`，不覆写目录迁移
的历史 evidence 或 `final-path-map.json`。主集成同时将 tests/helper/Go fixture 纳入默认 CI 结构门。

daemon 的 30 个文件已拆为 97 个主题测试模块及 30 个显式 support 模块；原测试定义及完整装饰器
逐块保留，不采用数字分片、星号聚合、动态执行或删空行凑门。共享 fixture 默认函数作用域及
autouse 注册通过同名 alias 明确导入；唯一 module-level anyio mark 原样显式绑定。1048 个测试定义、
384 个共享定义的 AST 对账通过；1165 个包含参数 ID 的用例 Counter 与拆分前完全一致。
同用例 setup/call/teardown 的 3494 条运行结果 Counter 也一致：1157 PASS、4 个既存 FAIL、
1 个既存 SKIP、1 个既存 XFAIL、2 个 XPASS，没有新增失败/错误或弱化断言。

daemon 全树目前最大代码文件 997 行、最大代码目录 7 个直接文件。其余两个 worker 的验收与
全树 9988 用例最终 Counter 由主集成统一收拢；单域通过不冒充全量产品功能验收通过。
