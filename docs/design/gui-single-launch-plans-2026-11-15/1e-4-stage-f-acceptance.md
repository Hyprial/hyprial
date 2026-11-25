# PR 1e-4: the eight org scenarios kept as the Stage F acceptance list

Date: 2026-11-16. Status: acceptance list, not an implementation. Owner decision
(README section "owner 决定汇总"): PR 1e-4 deletes the two browser scripts
`gui/tests/modules/dev04/org-lifecycle-browser.mjs` and
`gui/tests/modules/dev04/org-rework-browser.mjs`, together with the private
`/api/reference/*` host that served them. Both scripts were red on the base before
this PR (the reference org entry never issued `org.list`), they sat in the
`npm run verify` chain, and they got their data from nine `route.fulfill` calls
(ledger kind `browser-route-fulfill`). They could not be moved to the Contract
Mock because it has no example for the states below (see `1e-3-step0.md`, section
"Gaps"). The page behaviour they asserted is not covered by another browser
script, so this list is what Stage F (fixture replacement, `1e.md` section 3.5 and
section 5) must make testable again. The deleted code is recoverable from the
history of `dev` before this PR; the assertions are restated here so nothing
depends on reading it.

Each row names the product behaviour that must hold, the state the Contract Mock
or its replacement has to produce, and the viewports the old script used. A row is
accepted when a browser script that reaches data only through the daemon-request
path served by the Contract Mock (`gui/tests/support/contract-mock-gui.mjs`)
asserts it and passes.

| # | Old id | Behaviour that must hold | State the mock must produce | Viewports |
|---|---|---|---|---|
| 1 | L1+L2 (lifecycle) | While `org.join` is in flight the form shows a separate pending state (input and submit disabled, button reads "处理中…"); a synchronous double `requestSubmit()` sends no second `org.join` (exactly one request); after success the `org.list` refresh turns the empty list into the new org and the join result with its `.org-stepper` steps is kept ("已加入 review"). | A delayed `org.join` reply; `org.list` that returns `{orgs: [], leftOrgs: []}` before the join and one active member org (`group-review`, space `review-space`) after it (state or a sequence). | 375 |
| 2 | L3 (lifecycle) | A join reply that is returned but fails validation (`steps[0].kind` not a known step) is shown as "结果未知：加入请求可能已生效，未自动重试；请先刷新组织列表确认。"; no `.org-stepper`, no fake failed step, no retry button; exactly one `org.join` request. | An `org.join` result whose step kind is invalid (no declared example has one). | 375 |
| 3 | F2.1 (rework) | A failed `org.network` leaves the machines section in "状态未确认" with an in-page refresh button; one refresh recovers and shows the machine (`dev-alpha`); no automatic `org.fetch` is sent. | `org.network` fails on the first read and succeeds afterwards (a sequence). | 1440 and 375 |
| 4 | F3 (rework) | Switching the viewed org loads that org's detail (`org.network` with the second org's id) without detouring through the overview refresh; the machines page keeps a refresh action; no automatic `org.fetch`. | `org.list` with two orgs and a per-org `org.network` reply. | 1440 |
| 5 | F3.1 (rework) | A first read for the switched org that lands after a later refresh is discarded: the old machine name (`dev-beta-old`) never shows, the new one (`dev-beta-new`) does. | Ordering control: the first `org.network` for the second org is held and released after the refresh reply. | 1440 |
| 6 | F1 and F1b (rework) | `org.repair` preview with every candidate manual-only: "确认修复" is disabled and no `org.repair` with `apply: true` is sent. A repair that ran but whose device publish is incomplete (state `partial`) never shows "修复已完成。". | `org.repair` previews with only manual candidates and with a partial publish result (declared examples are only `repairable-candidate` and `empty-candidate-list`); `org.list` with a `partial` org. | 1440 |
| 7 | F4 (rework) | With the org source injected, the org provider is shared across pages: the Agent page's "所属组织" tab shows the org region; "添加可以发送的对象" with peer `agent-bob` sends one `link.add`, shows "已添加：<agent> 可以向 agent-bob 发送。"; "收回" sends one `link.drop` and shows "已收回。"; the page also read `agent.list`, `agent.secret.list` (`actor` = the agent), `agent.provider.default` and `link.list` (`from` = the agent); `link.add` and `link.drop` carry exactly `{peer: 'agent-bob', onBehalfOf: <agent>}`; no `org.fetch`. | `link.add` and `link.drop` change what `link.list` returns (state), next to the agent methods the page reads. | 1440 and 375x812 |
| 8 | F5 (rework) | When the catalog declares `org.create` unavailable there is no page-level write notice (`org-write-unavailable` absent); choosing "新建组织" in the org switcher shows exactly one warning toast ("新建组织暂不可用。"), opens no dialog, writes nothing into the page banner (`.org-live__message`), and causes no page or console error and no horizontal overflow at the narrow width. | A catalog in which `org.create` is not exposed (the mock publishes every HTTP-exposed method today; see #2855). | 1440 and 375 |

Notes for the Stage F author.

- The scripts also saved screenshots per step (`f2-1-unknown-*`, `f4-agent-link-*`);
  keep the same evidence names if the new script writes any.
- Rows 1, 3, 5 and 7 need state or ordering in the mock, which `1e.md` section 3.5
  and the open question on transitions already name; rows 2, 4, 6 and 8 need
  examples or a catalog variant only.
- KP-063 recorded the same list as the reason the two scripts were not migrated;
  that entry now points here.
