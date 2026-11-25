# GUI 模块边界检查（import boundary gate）

状态：随 WIP PR #2578 C1 引入；设计依据 [docs/design/gui-deep-module-structure-2026-11-08.md](../../docs/design/gui-deep-module-structure-2026-11-08.md)。
本门是**静态 import 边界检查**：它证明源码在模块边界上的依赖方向与消费方式符合规则，
**不证明运行时行为、打包产物内容、CSS/资源依赖图或功能正确性**。运行时与浏览器证据仍由
`npm run verify` 中既有浏览器契约与冒烟命令提供。

## 命令

```sh
npm --prefix gui run check:module-boundaries
node gui/scripts/check-module-boundaries.mjs --gui-root <dir>
node gui/scripts/check-module-boundaries.mjs --update-baseline
node gui/scripts/check-module-boundaries.mjs --update-baseline --accept-new
```

- `check:module-boundaries` 已纳入 `npm --prefix gui run verify` 的第一环。
- 退出码：`0` 通过；`1` 边界失败或拒绝更新；`2` baseline 文件本身不可用（未知键、非法 JSON、版本不符）。
  所有失败都是 fail closed：解析失败、读取失败、无法解析的 import 一律失败，不会静默放行。
- `--gui-root` 供隔离测试指向合成树，日常不需要。

## 检查范围与规则

解析 `gui/src/` 下全部 `.ts/.tsx/.mts/.cts/.js/.jsx`（含 `.d.ts`），用 TypeScript compiler API
提取静态 `import`（含 `import type`）、`export … from` 与动态 `import()`；注释不会进入 AST，
multiline import 与 JSX 内的动态 import 均被覆盖。相对路径与 `@/`（→ `gui/src`）别名都会解析到真实文件。

| 规则键 | 含义 |
|---|---|
| `feature-structure` | `src/features/<module>/` 必须有显式 `index.ts` 与含至少一个代码文件的 `impl/` |
| `cross-feature-deep-import` | 目标在 `src/features/<其他模块>/` 内的 import（静态/动态/type-only）必须精确指向该模块 `index.ts`，不得穿透 `impl/` 或子路径；模块内部相对导入不受限。该约束适用于 src 内所有消费者（pages、app、dev、其他 feature） |
| `prod-imports-dev` | `src/dev/` 之外的源码不得引用 `src/dev/**`。唯一内置豁免：`src/main.tsx` 以动态 `import()` 加载开发组合（设计规则 5 明确允许） |
| `src-imports-tests` | `src/`（含 `dev/`）任何文件不得引用 `gui/tests/**` |
| `feature-index-exports-mock` | feature 公共 `index.ts` 不得 import/re-export 解析到 mock/fixture 的文件（路径或文件名含 mock/fixture 记号）、`src/dev/`、`src/fixtures/` 或 `gui/tests/` 的目标 |
| `primitives-layering` | `src/components/primitives/` 不得依赖 `src/features/`、`src/pages/`、`src/dev/`、`src/state/`（业务状态） |
| `base-layer-references-feature` | `src/client/`、`src/platform/` 不得反向引用 `src/features/` |

语法错误（`parse-error`）与无法解析的引用（`unresolved-import`）是硬失败，**不能**进 baseline。

## baseline：只减不增

`gui/config/module-boundary-baseline.json` 精确逐项登记当前已知例外，不使用通配符或目录级豁免。
比较语义：

- **新增违规**（当前出现、baseline 未登记）→ 失败，列出 `NEW` 项；必须改代码，或走下面的显式登记流程。
- **stale 例外**（baseline 有、当前已消除）→ 同样失败，列出 `STALE` 项；B2/B4/C 阶段消除例外后必须收缩 baseline。
- 完全一致 → 通过。baseline 每条规则的例外数会打印在输出里，评审时可对照只减不增。

更新流程（可审查、不可静默并入）：

- `--update-baseline`：只做收缩（删除 stale 项）。若存在新增违规则拒绝写入并列出，不会把新增违规偷偷并入。
- `--update-baseline --accept-new`：允许把当前新增违规写入 baseline，逐条打印 `ADDED`，
  必须在 PR diff 中逐条评审。这不是常规路径，仅用于登记“当前树已知、待 B2/B4/C 处理”的例外。
- 存在硬失败（parse-error / unresolved-import）时任何更新都会被拒绝。

## 当前例外概况与退场责任

当前全部 13 个 feature 均有 `index.ts + impl/`，所有规则的登记例外均为 0：

- `feature-structure`、`cross-feature-deep-import`、`prod-imports-dev`、`src-imports-tests`。
- `feature-index-exports-mock`、`primitives-layering`、`base-layer-references-feature`。

正式 `App` 只负责 connected/live/unavailable 产品组合；fixture state、contract mock 和 development
session 集中在 `dev/FixtureApp.tsx` 与显式开发入口。共享壳和页面不 import dev，preview/live 继续复用
同一展示实现。`main.tsx` 动态加载开发组合是唯一的内置豁免。

`src/dev/` 内部自 2026-11-14 起再分两块，均为 `index.ts + impl/`：`dev/tools/` 是浮动工具层（`mountDevTools`、
`DevToolsLayer`、context 类型），`dev/fixture-tools/` 是仅限 fixture 组合的工具与开发预览 store。
依赖方向只允许 `fixture-tools → tools` 的类型导入；`tools` 可达的文件集合由 `tests/dev-tools/import-graph.test.ts`
按精确清单检查（本门的规则表不覆盖 `src/dev` 内部方向）。`dev/parity-composition.tsx` 只能动态导入 `./tools`。

2026-11-15（PR 4b-G）：`src/main.tsx` 另有一处允许的动态导入 `./dev/tools`（仅当会话响应带 `devTools` 时执行），
`verify:dev-product-parity` 的 `allowDevImportSpecifiers` 因此列出 `./dev/parity-composition` 与 `./dev/tools` 两项；
`check:module-boundaries` 的规则不变（对 `src/dev/**` 的动态导入本就是 `main.tsx` 的内置例外）。

新增违规则必须改代码；后续若确需登记新的组合例外，必须先经过逐条人工复核并使用 `--accept-new`，
在 PR 中说明理由。已消除的例外通过 `--update-baseline` 收缩，不能保留 stale 项。

## 未覆盖语义（本门不检查）

- `gui/tests/` 自身作为消费者的依赖方向（仅检查 `src → tests` 方向）。
- 运行时行为、动态加载结果、bundle 产物内容（由 build 与 production-copy 门覆盖）。
- CSS/图片/JSON 之外的资源引用语义；`require()`（仓库为 ESM，无此用法）。
- `src/components/` 下除 `primitives/` 外的目录（如 shadcn `ui/`）的分层约束。
- type-only import 与值 import 一视同仁计入边界（类型耦合同样登记，不设豁免）。
