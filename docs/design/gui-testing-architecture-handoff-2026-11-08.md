# GUI 测试、组件组合与架构边界改进交接

状态：WIP / handoff only，待其他开发者接手实施。日期：2026-11-08（Asia/Shanghai）。

用户已确认只创建 WIP PR 与交接文件，本 PR 不实施产品、测试或 CI 改造。
接手人先复核目标提交，再分批提交修复；文档中的候选方案不是已经完成的变更。

## 1. 基线、范围与现有 WIP

| 用途 | 精确提交 / 状态 |
| --- | --- |
| 分析时的 `dev` | `e8d38384372e06a14601090fece5833beeb92b58` |
| 实测的结构 WIP [#2578](https://code.hyprial.com/HyprialOS/hyprial/pulls/2578) | `98a0308901be62561332e73a0f1729e936309c13`，本次交接时仍 Open / Draft |
| 本次文档分支基线 | `dev@88df3a586383cc74c837505f982bedaaf76e62cd` |

#2578 已完成 `dev-mode/` → `dev/`、旧路径转发、重复聊天 controller 和旧 CSS 的清理。
所有 feature 的显式 `index.ts + impl/`、正式源码与 dev/test fixture 隔离、GUI import-boundary
检查仍是它的待完成工作。详细范围见该提交的
[结构迁移计划](https://code.hyprial.com/HyprialOS/hyprial/src/commit/98a0308901be62561332e73a0f1729e936309c13/docs/design/gui-deep-module-structure-2026-11-08.md)。
本线承接测试契约与验收方法，不另起一套目录迁移，也不重新引入已删除的 alias/controller。

交接时将 Dialog、Select、Field、底层 Select、对应 primitives/Select 测试、production-copy
测试、Vitest 配置、GUI workflow 和 CI plan 共 10 个文件与实测 WIP 比较，均为原字节相同。
这只证明相关源码未变，不代表在新 `dev` 上完成了运行时复测。组件问题不是 #2578 新增回归。
这期间 #2523 已合入 `dev`；组织与成员相关的旧 PR 运行记录不能作为本线新基线验收。

必读：[AGENTS.md](../../AGENTS.md)、[术语表](../../GLOSSARY.md)、
[前端组件规范](../guides/frontend-components.md)、
[单套 GUI 规则](../../gui/docs/ui-development-standard.zh-CN.md#single-gui)、
[共享入口](../../gui/docs/shared-ui-entry.zh-CN.md)、[已知问题](../reference/known-problems.md)。
使用当前术语 thread/conversation；存量 `sessions.*` 等标识按真实源码引用，不在测试重构中改变协议。

## 2. 已观察到的缺口

<a id="component-contracts"></a>

### 2.1 组件契约与组合（KP-036 / KP-037）

| 编号 | 复现 / 源码事实 | 影响与修复验收 |
| --- | --- | --- |
| C1 | Dialog 内打开 Radix Select 后按一次 Esc，父 Dialog 的 `onClose` 也调用一次；jsdom 和 Chromium 均复现 | 第一次 Esc 仅关闭菜单，保留表单；第二次才关闭 Dialog，焦点回到真正的触发按钮 |
| C2 | Select 公共适配层未把 `aria-label` 传给实际 combobox | 按可访问名称查找应成功；同时测试 `aria-labelledby`、disabled、ref 和描述属性 |
| C3 | Field 给 Select 注入的 `aria-invalid`、错误描述未到达 trigger | 真实控件具有错误状态、可访问描述及明确的 required 语义；验证错误清除后的恢复 |
| C4 | Field clone 子控件时覆盖了调用方已有的 `aria-describedby` | 合并并去重既有描述与 hint/error ID，不因新增 Field 提示丢失调用方说明 |
| C5 | 同一 Dialog + Select 浏览器探针中，菜单视觉上被 Dialog 覆盖；菜单 z-index 为 50，Dialog backdrop 为 1000 | 在真实浏览器中验证菜单展示、选项点击、焦点和局部截图；统一 portal 与层级策略 |

源码入口：
[Dialog](../../gui/src/components/primitives/Dialog.tsx)、
[Select 适配层](../../gui/src/components/primitives/Select.tsx)、
[Field](../../gui/src/components/primitives/Field.tsx)、
[底层 Select](../../gui/src/components/ui/select.tsx)、
[层级 token](../../gui/src/styles/tokens.css)、
[Dialog 样式](../../gui/src/styles/primitives.css)。
Dialog 监听 document 的 keydown，尚未与子弹层建立一致的 Esc/焦点处理规则。
SelectPortal 默认挂在 body；单独给某个页面增加 z-index 不构成统一修复。

C5 的 `elementFromPoint` 检查仍命中选项，不能将它记为“鼠标点击被阻断”。
此次得到的是视觉遮挡和层级不一致的证据。组件组合探针不等于全部生产路由均存在或已验收该场景。
当前 Field + Select 消费者也要逐项检查，不能从适配层缺口推导所有表单已经暴露同一错误。

现有测试的薄弱处：

- [Dialog 用例](../../gui/tests/primitives.test.tsx) 名称宣称恢复焦点，却没有真实 trigger 与关闭后的焦点断言；以 `fireEvent.keyDown(document)` 检查首尾跳转，不能覆盖完整浏览器焦点行为。
- [Select 键盘用例](../../gui/tests/modules/dev02/select-primitive.test.tsx) 初始值为 `claude`，最终只断言非空；即使选择未变化也满足断言。disabled 用例传了 `aria-label`，但只用匿名 role 查询，漏掉名称丢失。
- [shadcn 示例测试](../../gui/tests/shadcn-example.test.tsx) 有必要的 loading/submit 语义覆盖；`data-slot`、variant 和类名断言适合作为少量适配检查，不替代组合行为。

<a id="architecture"></a>

### 2.2 架构边界与测试耦合

实测 WIP 的临时 TypeScript 导入扫描发现 35 处跨 feature 深路径导入。扫描解析相对路径与
tsconfig 别名，覆盖 import、re-export 与字面量动态 import，计入类型边；目标不是
`features/<module>/index.ts` 的跨 feature 引用计为深路径导入。这是当时的诊断计数，不是新 CI 阈值。

例子包括 account 直接引用 onboarding 的展示实现、agents 直接引用 org/crosstab，以及多个
feature 直接引用 agent-chat 内部格式化函数。正式 App 仍静态引用多个 dev 实现；
agent-chat 公共入口仍导出 mock，mock 又引用 `tests/fixtures`。这些正是 #2578 后续范围。

现有 [client 边界测试](../../gui/tests/client/boundaries.test.ts) 主要匹配 `from` 文本且只扫描 `.ts`，
不能代表完整 GUI 的导入图约束。根 `check_module_boundaries.py` 的 Python AST 门也不能代替它。
应使用 TypeScript AST 与实际模块解析，测试别名、side-effect import、动态 import、re-export
和 type-only 依赖等情形；检查器必须用故意违规样例证明会拒绝，而非只在当前树上返回空结果。

测试边界应与模块归属一致：外部集成/公共契约测试通过 feature 公共 API；内部单测允许测试所属
模块的实现，不为测试方便扩大 index 导出。若迁入源码共置测试，必须同步 Vitest include 和生产排除，
不能让测试悄悄失去收集。跨模块调用全部经过 index 也不自动证明职责合理，仍要审阅依赖方向。

<a id="ci-artifacts"></a>

### 2.3 生产产物门与测试执行（KP-038）

[production-copy 测试](../../gui/tests/production-copy-gate.test.mjs) 读取 `dist/assets` 失败时
直接 return，测试被记为成功；空目录也缺少明确产物完整性校验。
[GUI workflow](../../.forgejo/workflows/gate-gui.yml) 的 gui-unit 先 test 再 build，构建后没有
直接执行 production-copy 脚本，却在 summary 中声称覆盖了该项。显式脚本调用位于
release-package，而 [CI plan](../../scripts/ci/ci-plan.mjs) 的 package 选择仍依赖旧
`src/gui` 是否存在。需要对具体事件的实际执行计划单独验证。

这是源码审阅确认的检查缺口。本次没有向远端 CI 注入违规产物，也没有证明生产包已经发生泄漏；
此次显式构建后的 production-copy 扫描为 PASS。现有禁用文案门继续保留，另用构建模块清单检查
禁止路径/模块，避免把“没有匹配到单词”当作完整的依赖隔离证明。

[共享浏览器入口](../../gui/tests/support/shared-views-browser.mjs) 将多个页面检查串到相同的
completeFirstRun 前置流程。KP-025 在“登录 Hyprial”处超时后，后续聊天/组织等场景没有执行到。
package 中部分浏览器文件被默认 test 排除，有单独脚本；应建立明确的收集与 CI 执行清单，
逐一说明在哪个 job 运行，不能把“文件存在”写成“CI 已覆盖”。

## 3. 后续实施切片与退出条件

以下为建议顺序，负责人由接手团队指定；本交接未启动任何切片。

| 切片 | 优先级 | 范围 | 退出条件 |
| --- | --- | --- | --- |
| T1 组件契约与弹层 | P0 | C1–C5；Dialog/Select/Field 的公共 API、键盘与属性透传 | 先保存旧提交的失败结果；修复后四个探针与既有组件测试通过，浏览器确认菜单层级、Esc 分层和焦点恢复 |
| T2 生产产物检查 | P0 | 缺失/空/损坏产物失败；新构建后强制执行检查；核实 GUI scope/job | 合法包通过，缺产物和禁止模块样例失败；PR 的实际 CI 计划包含该检查且 summary 与执行一致 |
| T3 模块与公共面 | P1，协调 #2578 | import-boundary、显式导出、dev/test 隔离与测试迁移 | 违规样例被拒绝；例外只减不增；正式 bundle 无 dev/test 可达路径，公共消费者与共享页面回归通过 |
| T4 测试执行与前置 | P1 | Node/jsdom/browser 分工、收集清单、KP-025/026 与公共 harness | 首次设置有独立 E2E；其他页面通过明确的就绪入口独立运行；失败有 trace/截图/日志，未运行项如实报告 |
| T5 视觉与无障碍 | P2 | light 主题、360/390/430px 与桌面、长中文、reduced motion、axe | 固定字体/浏览器等环境的局部视觉基线和键盘断言通过；自动扫描与人工未测范围分开记录 |

T1 优先评估把 Dialog 内部交互统一到与现有 Select 一致的 Radix 机制，保留项目 primitives
公共语义；选择其他实现需说明弹层栈、焦点、portal 和关闭策略。这里不预先要求整站组件替换。
保留 Button 的默认 type、loading 防重、variant/尺寸映射，Select 空值哨兵和受控值语义，
以及 Dialog 的显式确认、关闭策略与 pending 状态；不要顺带升级 React/Tailwind 或扩大依赖。

T3 在 #2578 路径稳定后更新测试 import。不要从旧树整文件覆盖新结构；搬迁本身不应改变共享
view、controller 或后端操作语义。fixture/test 的公共样例归属与当前业务线 owner 协调。
正式与开发复用相同视图，可信 principal 仍由宿主提供；普通生产入口不绕过 bootstrap，
不复制 preview/live 页面，不为了浏览器通过而伪造业务成功。

## 4. 推荐的测试契约

| 层 | 工具 / 环境 | 应验证的内容 |
| --- | --- | --- |
| 纯状态与业务控制 | Vitest Node | 成功/失败/未知、并发和取消、旧 generation 结果、防重复写入、草稿/操作键恢复 |
| primitive 与 feature 公共行为 | RTL + jsdom | role/name、标签、字段错误、disabled/loading、确切回调和值、表单语义 |
| 组件组合 | 真实浏览器 | Dialog + Select + Field；真实 Tab/Shift+Tab/Esc、portal、显示和焦点恢复 |
| 布局与样式 | 浏览器和局部视觉回归 | computed style、长文与窄屏、滚动归属、焦点轮廓、动效、层级与字体 |
| 宿主与业务集成 | Contract Mock 与隔离真实 daemon 分开 | 同一共享 App/view/client 路径、可信启动会话、声明方法、真实响应与恢复；保留不同证据级别 |
| 架构规则 | AST/模块解析静态门 | 公共入口、依赖方向、禁止 dev/test 和业务反向依赖、禁止规则的负例 |

- 默认用 `getByRole(..., { name })`、`getByLabelText` 和 `userEvent.setup()`；异步 UI 用 `findBy*` / 有界条件等待。`fireEvent` 用于确需底层事件的场景，不以任意 sleep 或增大 timeout 代替状态同步。
- 替身放在传输/业务端口边界，保留真实 controller 与控件；复用已有 Contract Mock/生成客户端，避免每个测试各造一份不一致 DTO。
- CSS 文本/AST 规则可作为 lint 或架构测试保留；`css: false` 的 jsdom 不验证布局。不要把全部 CSS 类名锁成组件契约，也不因上游 Radix 已测试就省略本地组合验证。
- 保留现有未知结果、防重复写入和真实 loopback 测试。不要为统一 runner 丢失这些覆盖；不以全局覆盖率数字取代关键状态和组合验收。
- 可以使用现有 Playwright，或在单独评审后采用 Vitest Browser Mode；先统一必要的 harness、生命周期与报告，不先全面更换测试框架。
- axe 等自动扫描补充标签、重复 ID、对比度检查；人工键盘和读屏范围仍单独记录。只验现有 light 主题，不把暗色、WebView 或移动真机写成支持。

## 5. 最小失败探针

以下代码仅记录当时的诊断方法，不是本 PR 已加入主测试集的测试。接手人在独立 worktree 中
临时保存为 `gui/tests/gui-handoff-probes.test.tsx`，用当前公共入口调整 import 后运行。
不要把有意失败的诊断文件混入交付的主 collector；真正修复应在独立切片先红后绿。

```tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Button, Dialog, Field, Select } from '../src/components/primitives';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const options = [
  { value: 'claude', label: 'claude' },
  { value: 'codex', label: 'codex' },
];

it('Select preserves its accessible name', () => {
  render(<Select aria-label="选择 harness" value="claude"
    options={options} onValueChange={() => {}} />);
  expect(screen.getByRole('combobox')).toHaveAccessibleName('选择 harness');
});

it('Field preserves the consumer description', () => {
  render(<>
    <p id="external-description">调用方提供的说明</p>
    <Field label="名称" htmlFor="name" hint="字段提示">
      <input id="name" aria-describedby="external-description" />
    </Field>
  </>);
  expect(screen.getByLabelText('名称').getAttribute('aria-describedby')?.split(' '))
    .toContain('external-description');
});

it('Field metadata reaches a composite Select', () => {
  render(<Field label="harness" htmlFor="harness" hint="选择一个"
    error="此选项不可用" required>
    <Select id="harness" value="claude" options={options}
      onValueChange={() => {}} />
  </Field>);
  const trigger = screen.getByRole('combobox', { name: /harness/ });
  expect(trigger).toHaveAttribute('aria-invalid', 'true');
  expect(trigger).toHaveAccessibleDescription(/此选项不可用/);
});

it('Select Escape keeps the parent Dialog open', async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  function Harness() {
    const [open, setOpen] = useState(false);
    return <>
      <Button onClick={() => setOpen(true)}>打开表单</Button>
      <Dialog open={open} title="表单"
        onClose={() => { onClose(); setOpen(false); }}>
        <label htmlFor="inside-harness">harness</label>
        <Select id="inside-harness" value="claude" options={options}
          onValueChange={() => {}} />
      </Dialog>
    </>;
  }
  render(<Harness />);
  await user.click(screen.getByRole('button', { name: '打开表单' }));
  await user.click(screen.getByRole('combobox', { name: 'harness' }));
  expect(screen.getByRole('listbox')).toBeInTheDocument();
  await user.keyboard('{Escape}');
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog', { name: '表单' })).toBeVisible();
});
```

```sh
npm --prefix gui test -- --reporter=verbose tests/gui-handoff-probes.test.tsx
```

浏览器复现：在隔离测试入口用真实 createRoot 渲染上述受控 Harness，加载
`src/styles/tokens.css` 与 `src/styles/shadcn.css`，通过 Vite 随机 loopback 端口打开，
不连接 daemon/账号。点击打开表单 → 打开 Select → 保存菜单打开时截图 → 按 Esc。
当时 Chromium `156.0.8078.79` 观察为：

```json
{
  "selectZ": "50",
  "dialogZ": "1000",
  "optionReceivesPointer": true,
  "afterEscape": {
    "dialogCount": 0,
    "listboxCount": 0,
    "parentCloseCalls": 1
  },
  "pageErrors": []
}
```

修复后的浏览器用例还应验证：选项确实切换到 `codex` 且只回调一次；第一次 Esc 后
Dialog 保留且焦点回到 Select trigger；再次 Esc 关闭后焦点回到“打开表单”；验证
pending/disabled、空值往返、字段错误及菜单无遮盖。截图应与行为断言配合，不只检查 ARIA 存在。

## 6. 实测记录与复核命令

分析环境：Linux，Node `v24.22.1`、npm `11.9.0`；运行对象为 §1 的 WIP 精确提交。
全部复现使用隔离副本和临时资源。未连接生产 daemon、真实账号或外部业务服务。

| 检查 | 本次结果 | 范围 |
| --- | --- | --- |
| `npm ci --ignore-scripts` | PASS | 隔离副本的锁定 Node 依赖 |
| 原有聚焦测试 | PASS，7 files / 102 tests | 下列七个文件，未包含后来增加的临时探针 |
| `npm run build` | PASS | 含 `tsc --noEmit` 与 Vite production build |
| 显式 production-copy 脚本 | PASS，0 forbidden markers | 刚构建的 dist；不证明结构性 dev/test 隔离 |
| §5 四个探针 | FAIL，4/4 | 名称、Field 描述合并、Field + Select 错误、子菜单 Esc |
| Chromium 组合探针 | 已复现缺陷 | C1 / C5；不是完整产品或业务路由验收 |
| `linked-preview-browser.mjs` | FAIL；后续场景 BLOCKED | 等待“登录 Hyprial”30 秒超时，匹配已登记 KP-025 |
| 全量 GUI / Python / 真实 daemon / 原生与真机 | NOT RUN / UNKNOWN | 没有本线运行结论；不引用 #2578 的 1709 项记录冒充本线全量实测 |
| 新文档基线运行时复测 | NOT RUN | 仅完成相关 10 个文件原字节比较 |

从仓库根重跑当时的聚焦检查：

```sh
npm --prefix gui ci --ignore-scripts
npm --prefix gui test -- --reporter=dot \
  tests/primitives.test.tsx tests/shadcn-example.test.tsx \
  tests/modules/dev02/select-primitive.test.tsx tests/token-contract.test.ts \
  tests/client/boundaries.test.ts tests/ui/button-ast-contract.test.ts \
  tests/production-copy-gate.test.mjs
npm --prefix gui run build
node gui/scripts/verify-production-copy.mjs gui/dist
HYPRIAL_BROWSER_EXECUTABLE=/usr/bin/chromium node gui/tests/linked-preview-browser.mjs
```

浏览器可执行路径按实际环境替换；必须记录 Node/npm/浏览器版本、精确 SHA 与所选数据源。
修改产品或测试结构前按 AGENTS.md 先运行结构/边界门；修复后先聚焦再按范围跑全量：

```sh
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
npm --prefix gui run typecheck
npm --prefix gui test
git diff --check
```

真实 loopback/CLI/browser 检查需先按当前仓库指引准备 Python 依赖、短临时 socket 路径和独立
home/state；不直接继承操作员凭据。KP-025/026 与新基线的 KP-034/035 分别保留，
不删除断言、放宽超时或改为 skip 来制造通过；记录 PASS/FAIL/BLOCKED 和未执行范围。

## 7. 接手完成清单

- [ ] 读取最新 dev 与 #2578 状态，在记录中写明精确 SHA、路径变更与已知问题编号；检查 KP 编号并发冲突。
- [ ] 与 #2578 协调公共面和测试归属，选择独立小切片；先复现 C1–C5，再审查实现选择。
- [ ] T1/T2 分别保存修复前失败、修复后通过及浏览器/CI 实际执行证据，关闭对应 KP 条目时保留历史。
- [ ] T3 具备静态门负例、正式 bundle 依赖证据和公共面测试，不以改目录、空 impl 或扩大导出代替边界。
- [ ] T4/T5 记录收集清单、共享视图路径、双入口能力差异和未测范围；按现有 UI 交接模板交付。
- [ ] 实施 PR 明确 docs/test/product/CI 改动范围，按需同步 contract/skill，保留 Draft 直到其自身退出条件满足。

## 8. 官方依据

- [React：推荐 Testing Library](https://react.dev/warnings/react-test-renderer)
- [Testing Library：用户行为原则](https://testing-library.com/docs/guiding-principles/)
- [Testing Library：语义查询](https://testing-library.com/docs/queries/about/)
- [user-event：完整交互与 setup](https://testing-library.com/docs/user-event/intro/)
- [shadcn/ui：源码交付与组合](https://ui.shadcn.com/docs)
- [Radix Select：交互与可访问接口](https://www.radix-ui.com/primitives/docs/components/select)
- [Playwright：行为、隔离与定位](https://playwright.dev/docs/best-practices)
- [Playwright：自动与人工无障碍验证](https://playwright.dev/docs/accessibility-testing)
- [Vitest Browser Mode](https://vitest.dev/guide/browser/)

以上建议是结合官方原则与仓库证据的本线判断；并非 shadcn 官方发布的一套专用测试规范。
