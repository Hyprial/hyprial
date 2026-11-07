# Connect to an operator-approved service

`hyprial org service` creates loopback-only TCP mappings through the daemon's
existing Tailcat sidecar. A catalog entry only names and documents a service;
the daemon's protected access record must separately approve the exact device,
server public key, generation, and remote port. The service host independently
admits caller public keys, and the application still owns its own API keys.

## Before connecting

An operator must provide all of the following without placing private values in
the catalog, command line, environment output, logs, or repository evidence:

- a capable platform wheel whose protocol-v3 hello includes
  `service-connect-v1`;
- an operator-built generic host image from
  `sidecar/hyprial-tailcat/deploy/Dockerfile`, a durable host key, and an
  approved caller-key allow-set;
- a reviewed public catalog at the root of the pinned OrgFS services space, or
  an explicit builtin-only decision;
- an atomically installed
  `$HYPRIAL_HOME/state/tailcat/service-access.json` in a `0700` directory as a
  regular `0600` file owned by the daemon account; and
- any application credential delivered through that application's normal
  secret/grant workflow.

The protected record is operator configuration. Do not send it through chat,
put it in a public OrgFS document, or copy its address or server key into a bug
report. Missing, unreadable, invalid, disabled, or stale records fail closed;
there is no last-good private-record fallback.

An unconfigured daemon stays inactive: with neither this record nor persisted
service intents it starts no service-connect worker or polling cadence. Installing
the record later requires `hyprial daemon restart` or any `hyprial org service ...`
call to activate the manager; the daemon does not poll the absent file.

The source-only host package is generic TCP, not a sub2api-specific image:

```sh
cd sidecar/hyprial-tailcat
docker compose -f deploy/compose.yaml build host
docker compose -f deploy/compose.yaml run --rm --no-deps host \
  genkey --out /var/lib/hyprial-tailcat/host.key
APP_IMAGE=example/app:operator APP_INTERNAL_PORT=8080 TAILCAT_PORT=18080 \
  docker compose -f deploy/compose.yaml up -d app host
```

Replace the example's empty `clientPublic` set before expecting access. Empty
means deny all; `allowAny` must remain false. The app port is not published to
the host. Plain TCP is the default and receives no PROXY header.

With the compose deployment, recreate the host container after changing the
caller allow-set. That restart is the operational revocation path; do not rely
on SIGHUP alone as proof that a caller's existing streams were revoked.

## Use a mapping

Inspect public guidance, connect, and manually export only the printed public
environment hint:

```sh
hyprial org service guide api --full
hyprial org service connect api
export ANTHROPIC_BASE_URL='http://127.0.0.1:18080'
hyprial org service list
```

The daemon prints environment text but never modifies the current shell or
injects application keys. Fixed ports are strict: if the remembered or catalog
port is occupied, the intent remains visible as failed and the daemon does not
silently move it. `lastDialMs` is TCP dial duration, not RTT or application
health. `path=unknown` is an honest result when Tailcat has no authoritative
path observation.

Catalog `auth` and guide prose are untrusted operator-facing text: every writer
in the trusted OrgFS space can change them. Review the displayed text before
following it, and never treat it as a secret-delivery or command authority.

The local listener has the same baseline-C trust boundary as a conventional
port forward: it binds `127.0.0.1` without per-UID authentication, so any local
process that can reach the port can use the daemon's Tailcat identity. Shared
hosts must rely on the application's own key/authentication or must not use the
mapping. Each service mapping admits at most 64 concurrent local connections;
excess connections are closed before an upstream dial.

Existing streams do not survive daemon, sidecar, host, or record rotation.
After an outage, open a new local connection to observe recovery. A successful
mapping proves a listener and transport path, not HTTP status, model behavior,
or application authentication.

Remove the durable intent and mapping explicitly:

```sh
hyprial org service disconnect api
```

Before downgrading to a pre-service-connect binary, disconnect every service.
Older binaries ignore the new JSON keys but do not clear the SQLite service
tables, so otherwise stale intents can return after a later re-upgrade.

Use `hyprial daemon status --json` for daemon status. Bare `hyprial org service`
shows service help and exits 2. The old `hyprial service` path also exits 2,
but emits only `moved: hyprial service  →  hyprial org service` on stderr.
Neither path is an alias for daemon status; `hyprial daemon service` manages
the local supervisor.

## Acceptance boundary

Unit, fake-sidecar, loopback relay, and Docker-build checks do not prove a live
service. Attended acceptance additionally requires approved disposable hosts,
the protected record, actual caller revocation, concurrent mesh plus multiple
service streams (including same-DERP-region relay), record rotation/disable,
restart on the same port, and a real streamed application request with a
separately provisioned app key. Report any absent prerequisite as `BLOCKED`,
never as a skipped pass.
