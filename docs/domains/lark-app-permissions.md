# Lark App permission inventory and authorization

`hyprial adapter scopes check <name> --json` inventories the configured App's tenant
scopes and reports each gateway capability as `granted`,
`declared_ungranted`, or `undeclared`. The inventory includes
`im:message.group_msg`: it is optional for basic mention delivery, but group-
chat history recovery after a websocket outage requires both that scope and
one base message-read scope (`im:message` or `im:message:readonly`). Doctor's
`missingGroups` identifies which half is absent.

`hyprial adapter scopes authorize <name> --json` calls Feishu's
`POST /application/v6/scopes/apply` for scopes that the App already declares
but the tenant has not authorized. It returns an official clickable developer
console URL. The adapter also recognizes structured `permission_violations`
on error 99991679 and 230027, applies one App-wide one-hour lease to automatic
requests, and sends the administrator hint to the configured default
direct route. That notification uses a recovery-free SDK client so a missing
send scope cannot recursively request or notify forever.

The one-hour lease is App-wide (not per scope), process-safe, and persisted
beneath the shared Lark adapter state. Daemon route delivery and adapter
workers therefore cannot race duplicate apply calls, and a restart does not
reset the lease. Malformed state is quarantined without exposing its content;
clock values more than five minutes in the future are treated as invalid rather
than throttling forever. If lease persistence itself fails, recovery still
attempts the administrator request and notification, returns a sanitized
`persistenceWarning`, and marks `duplicateApplyRisk: true` because another
process or restart may repeat the apply call.

Error 230027 alone is not treated as proof of a missing App scope: Feishu also
uses it for user authorization and bot-sharing restrictions. Automatic
authorization runs only when the response includes a validated missing-scope
subject. Returned links are accepted only from official Feishu/Lark hosts.

## Manual boundary

The v6 apply API does **not** add undeclared scopes. Plain
`hyprial adapter scopes authorize` therefore cannot declare a scope; declaration happens
either in the developer console or through `hyprial adapter scopes authorize
--interactive`, whose device-authorization link declares and authorizes the
scopes in one confirmation. App version publication, tenant-admin approval,
availability scope, enabling bot capability, and adding the bot to a chat also
remain manual. Neither `doctor` nor `authorize` creates an App or grants its
own request.

Creating an App is not part of this manual list: `hyprial adapter add` runs
the official device-authorization flow to create (or re-authorize) an App
programmatically, with one human browser/QR approval as the only manual step.
See [lark-app-onboarding.md](../guides/lark-app-onboarding.md).

Trusted GUI hosts can reuse the public Python
`create_lark_authorization_service(home, state_dir)` facade. It provides
bounded App-scope inventory, authorization start/read/cancel, and host-local
operation recovery using owned processes; it does not invoke the CLI or user
OAuth. The authoritative states and lifecycle are specified in
[the authorization service contract](../../contract/lark-authorization/README.md).
