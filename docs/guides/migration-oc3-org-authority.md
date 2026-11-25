# OC3 organization authority migration

Back up the organization cell, OrgFS directory, and Tailnet receipts before
upgrading. The first owner daemon seeds the Tailnet roster from its cell
device entries and records the resulting authority revision.

During migration, do not run a Casdoor and cell dual-write. A completed seed
is forward-only. If a step fails, retry the same owner operation using its
durable operation receipt. Restore the backup and rerun the migration when a
rollback is required.
