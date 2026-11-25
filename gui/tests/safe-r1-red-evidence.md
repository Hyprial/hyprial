# SAFE-R1 RED 证据

结论：新增测试在基线 `98b104257485a88236c569d293c50e7acc909fb0` 上按预期失败。失败点对应尚未写入的 SAFE-R1 行为。

## 定向测试

命令：

```text
npm exec -- vitest run tests/safe-r1-ui.acceptance.test.tsx tests/safe-r1-layout-contract.test.ts tests/safe-r1-dev-modules.acceptance.test.tsx
```

结果：3 个文件均失败。9 项中 1 项通过、8 项失败。失败项为：

1. 页面仍显示蓝紫渐变。
2. 应用外框仍随页面增长。
3. 760 宽度仍改用整页增长。
4. 主导航仍显示“设置”。
5. 产品外框内仍显示演示、样例、fixture 或占位服务。
6. `DevModuleIndex` 尚不存在，2 项模块索引测试失败。
7. 聊天工作区仍保留卡片边距、边框、圆角和自适应高度。

聊天根节点已经直接位于 `.content-wide`，对应的结构检查通过。CSS 仍保留边距、边框、圆角和自适应高度，因此完整聊天布局门仍未通过。

## 双构建检查

命令：

```text
node tests/safe-r1-build-contract.mjs
```

development 和 production 均完成构建。检查随后按预期失败：

- development 缺少开发模块索引、dev-01 至 dev-06 和“暂不可用”。
- production 仍包含 `FIXTURE`。

## 基线完整验证

首次并行运行 `npm run verify` 时，17 个测试文件中 16 个通过。114 项中 112 项通过、2 项失败。失败来自并行负载下的旧测试：1 项超过 5 秒，1 项输入事件顺序异常。测试代码、超时和生产代码均未为此改动。最终集成验证必须在其他工作树停止 Vitest 后单独重跑。
