# 1f — owner decisions (2026-11-14)

The owner accepted the rule in plan §0 and the planner's recommendations, with one overriding instruction: "delete everything that can be deleted". Apply the plan with these answers to §2.11:

| Q | Decision |
|---|---|
| Q1 | As recommended: remove the `只读` labels in Tasks and `MessageSessionsSection`; keep it on `能力授权`. |
| Q2 | Hide the `任务` nav entry and route `tasks` to `start`; DELETE the placeholder `TasksPage` itself rather than keeping an empty page. |
| Q3 | Not rendered: hide the `通信` view toggle when the model has no communication data. |
| Q4 | Keep as a product rule, reworded to `个人组织不能发出邀请。` |
| Q5 | Remove the parenthesis; show the raw value only. |
| Q6 | Keep inline (result of an operation inside a multi-step flow, carries the retry action). |
| Q7 | Remove `· Squire 回复未确认`; keep `本地首次设置流程已完成`. |
| Q8 | The org page-local banner is not the regular mechanism; migrate only `unavailable` results now. Converging the rest is a separate later change. |
| Q9 | DELETE the orphan (unmounted) components and their tests instead of editing them. |
| Q10 | DELETE the `UnsupportedState` primitive and the `unsupported` member once no user remains; update the standard's state list. |
| Q11 | Keep `DevDataSourceNotice`. |
| Q12 | Reword to `未配置`. |
| Q13 | As recommended: toast text `${subject}暂不可用。`, tone warning; technical reason only as the entry source in the notification center. |
| Q14 | Confirmed: values driven by a real read stay. |

Where the plan offers "edit or delete" for dead code, choose delete.

## Feature-area plan (1f-areas.md) — owner questions, resolved 2026-11-14

Resolved by the orchestrator under the owner's standing instructions ("delete everything that can be deleted"; no capability → not rendered). The owner is informed and may still override:

| Q | Decision |
|---|---|
| 1 | Hide the Agent and 组织 nav entries when the composition has no source at all (same mechanism as Tasks). |
| 2 | Formal org page: controls whose port the live source never implements are NOT RENDERED (设为当前组织, 采信, 校验, 发布候选, 模板导入, 改名, 审批 segment, received-invite 加入, inbound-migration decisions). |
| 3 | Delete the sections left unmounted since #2727 together with their tests; keep the API ports for a later clean-up. |
| 4 | Remove `accountLoginDisabled`; 登录 notifies on use. |

## F6 additions (owner, 2026-11-15)

- `dev01-onboarding-browser` never ran in CI: `describe.skipIf(!hasBrowser)` and gate-gui.yml runs `npm --prefix gui test`
  (l.260) before Chromium is provisioned (l.281). Locally it fails first on `gui/tests/dev01-visual-font.mjs`
  (only `DEV01_CJK_FONT` and four hard-coded WSL paths), then, with a font supplied, on
  `assertLayout` (`gui/tests/dev01-onboarding-browser.mjs:91`): primary button background never equals `rgb(26, 22, 32)`.
  Same failure on eadb831d7 (pre-F0), so pre-existing.
- F6 must: (1) make the font lookup find Linux system CJK fonts (fc-match or common paths; keep the env override);
  (2) find why the colour assertion fails and fix test or product so the test passes;
  (3) move Chromium provisioning before `npm test` in gate-gui.yml so the test really runs in CI, and remove the
  skip-on-no-browser in CI (a missing browser in CI must fail, not skip). Update tests/tooling/ci planning tests.
