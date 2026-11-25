# OC7 organization lifecycle migration

This guide covers the three OC7 lifecycle changes: legacy organizations
without an org cell (D5), org deletion, and tailnet authority
shutdown/switchover (M2). Back up the organization cell, OrgFS directory, and
Tailnet receipts before any destructive step.

## Legacy organizations without an org cell (D5)

Organizations created before OC3 have no org cell, and none is ever
backfilled: `hyprial org repair --apply`, running `hyprial org create <org>`
again on the existing org, invites and roster reads all leave it without one.
On the owner's machine, member operations on such an org (`hyprial org
invite`, `leave`, `remove`, and owner-side leave processing) fail with
`ORG_CELL_UNBOUND`, and `hyprial org network --json` lists it under
`dataProblems` with reason `org-cell-unbound`; `hyprial doctor` reports the
same through the `org-metadata` check.

Only the owner's machine can tell whether the org cell exists. Member machines
never hold org cells, so they do not see `ORG_CELL_UNBOUND`; their `leave`
takes the normal path, and once the owner deletes the org it reduces to local
cleanup.

Two ways out, both explicit:

1. Rebuild (when the org is still needed):
   1. On the owner's tailnet authority device, delete the legacy org (see
      [Org deletion](#org-deletion)).
   2. Create the replacement under a **new** org name, for example
      `hyprial org create group-acme2`. The old name cannot be reused on this
      home: its directory space still exists with the `deleted` marker, and
      `hyprial org create` with that name fails with `ORG_ALREADY_DELETED`.
      There is no supported way to clear these tombstones.
   3. Invite every member to the new org; each member joins through the
      fresh invite and, on their own machine, runs `hyprial org leave <old
      org>` to drop the old one.
2. Delete (when the org is obsolete): run step 1 alone.

## Org deletion

`hyprial org delete <org>` runs only on the device that holds the org's
tailnet authority (the owner's authority device). Any other device is refused
with `AUTHORITY_REQUIRED` before anything is written; move the authority
first with `hyprial org repin` if that device is gone (see below).

It is a two-step, journaled operation:

1. The preview binds a durable `operationId` to the exact tailnet revision,
   authority and device set. Running the preview again returns the same
   operation while nothing changed, and binds a fresh one after any roster
   change. A declined or refused preview has no lasting effect: it does not
   block `hyprial org create`.
2. Confirmation with `--yes` (or an interactive TTY confirmation) first
   re-reads the tailnet. If the revision, authority or device set moved, it
   fails with `REVISION_CONFLICT` and nothing is deleted; re-run the command
   to preview and confirm the fresh operation. Otherwise it executes: the
   OrgFS org metadata is tombstoned (`state: "deleted"`), the tailnet is shut
   down terminally (all members tombstoned, invites revoked, backup written
   first), pending removal journals are dropped, and a local deleted-org
   record is kept.

If a step fails, re-run the same command: the journal resumes at its phase.
If the tailnet still moves between the check and the shutdown, the tombstone
is already written; re-running `hyprial org delete <org>` on the authority
previews and finishes it. The same command also finishes an org whose
`deleted` marker was written by an older build while its tailnet is still
open.

Deletion needs the tailnet sidecar. On a home that is not network-isolated,
`org delete` and the deleted-org `org leave` refuse with
`MEMBERSHIP_NOT_INITIALIZED` before anything is written when the sidecar is
not running; start the daemon's forwarding and re-run. An earlier build
treated a missing sidecar as "no tailnet" and could report `complete` with the
tailnet still open; re-running `hyprial org delete <org>` on the authority
now previews that org again and shuts the tailnet down.

The authority keeps the closed tailnet after deletion so that members which
were offline or slow can still fetch the closed roster and learn of the
deletion (OrgFS sync between devices rides tailnet admission, so the marker
alone may not reach them). The owner drops it from the authority device with
`hyprial org leave <org>` once members have had time to see it. `leave` on
the authority is refused with `LAST_AUTHORITY_REFUSED` while the org's
tailnet is still open: finish `hyprial org delete <org>` first.

Members observe either the `deleted` marker or the closed roster; their
`hyprial org leave <org>` then reduces to local cleanup (no removal request
to the owner). Until then `hyprial org network --json` lists the org in
`dataProblems` with reason `org-deleted` (path `directory/org.json` for the
marker, `tailnet` for a closed roster) and `hyprial doctor` warns about it.

Deletion never resurrects: creating an org with a name this home already
deleted fails with `ORG_ALREADY_DELETED` once the tombstone is committed
(deletion journal), and while the deleted-org record or the OrgFS `deleted`
marker exists.

## Sub-orgs and deletion

A sub-org (`hyprial org create <child> --parent <org>`) shares its root
ancestor's tailnet, so deletion follows two extra rules:

- **Leaf-first.** `hyprial org delete <org>` fails with `OPERATION_CONFLICT`
  while any direct sub-org of `<org>` is not deleted; the message names them.
  Delete the deepest sub-orgs first, then their parents. A sub-org created
  between the preview and the confirmation is caught at confirmation, before
  anything is written. A child is recognised from its `directory/org.json`,
  from an interrupted create, or from the owner's org cell; an org whose
  `directory/org.json` cannot be read also blocks the delete (fix or delete
  that org first).
- **The shared tailnet stays open.** Deleting a sub-org never shuts down or
  forgets the inherited tailnet. It runs on the authority device of that
  tailnet (a device without that tailnet gets `AUTHORITY_REQUIRED`),
  tombstones the sub-org (`state: "deleted"`), revokes every member except
  the owner from the sub-org's OrgFS directory and ACL spaces, and retires
  its org cell. A sub-org whose `deleted` marker exists but which this home
  never finished deleting is previewed and finished by the same command. The preview and the result report
  `tailnetPhase: "inherited"` and `tailnetClosed: false`, and no revision or
  device set is bound, so activity elsewhere on the parent tailnet does not
  force a re-preview. Members keep their parent-org membership. Only deleting
  the root org shuts the tailnet down.

## Tailnet authority shutdown and switchover (M2)

Roster writes (org cell `members`/`org.members`) are only accepted on the
device that currently holds the tailnet authority; any other device fails
with `ORG_ACTION_NOT_AUTHORIZED`. When the authority device is lost or
replaced:

1. Stop the daemon, then run `hyprial org repin` to atomically move the
   tailnet authority to an active member device (durable plan, backup,
   `REVISION_CONFLICT` fail-closed without writing).
2. Restart the daemon on the new authority device. It takes over roster
   writes immediately; the old device is refused. Org cell updates keep
   their `expect_revision` conflict handling — a stale revision fails closed
   without writing, so re-read and retry.

Sub-orgs follow the authority of the tailnet they inherit: roster writes
and owner-side member removal on a sub-org are refused with
`ORG_ACTION_NOT_AUTHORIZED` off that authority device, or when this device
does not hold the inherited tailnet at all. A member leaving a sub-org is
not gated.

While the authority device is unavailable the roster is frozen: join and
removal requests queue, and members already in the roster are unaffected.
Orgs without a tailnet (network-isolated homes) have no authority to check
and stay writable.
