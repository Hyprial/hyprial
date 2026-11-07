# Generic Docker service host

This source-only image supervises the existing Tailcat engine for an arbitrary
TCP application. Build it from `sidecar/hyprial-tailcat`; no registry image is
published by this repository.

The host config contains only absolute paths, public caller `clientPublic`
keys, and exposures. `allowAny` must remain `false`; an empty allow set denies
all callers. Targets are loopback TCP (`tcp:127.0.0.1:<app-port>`) or an
absolute Unix socket. Plain TCP is the default and does not receive a PROXY
header. `proxyProtocol: "v2"` is opt-in for an application that explicitly
understands it, and is accepted only for a private Unix socket target: an
absolute, clean `unix:` path whose parent directory is a real (non-symlink)
directory owned by the container user with mode `0700` and no setuid, setgid
or sticky bit. A `v2` exposure with a TCP target, or with a socket directory
that fails these checks, is rejected at startup.

Provision the durable host key once in the private volume. This command is
intentionally separate from host startup and never regenerates an existing
key:

```sh
docker compose -f deploy/compose.yaml build host
docker compose -f deploy/compose.yaml run --rm --no-deps host \
  genkey --out /var/lib/hyprial-tailcat/host.key
```

Set `APP_IMAGE`, `APP_INTERNAL_PORT`, and `TAILCAT_PORT` for the operator-built
application image and its internal/exposed ports, then replace the empty
`clientPublic` set with approved caller public keys. Start the app and host:

```sh
APP_IMAGE=example/app:operator APP_INTERNAL_PORT=8080 TAILCAT_PORT=18080 \
  docker compose -f deploy/compose.yaml up -d app host
```

The host shares only the app container's network namespace
(`network_mode: service:app`); it publishes no app port, uses no Docker socket,
host networking, privilege, `NET_ADMIN`, TUN, or public control socket. The
host filesystem is read-only except for the private state volume. The runner
owns `Up`, `Expose`, admission, validated SIGHUP reload, and `Down` on SIGTERM.
For caller revocation under compose, update the allow-set and recreate the host
container. Container recreation, not SIGHUP alone, is the operational
revocation path and breaks existing streams.
It does not create a daemon or a second inbox.

`host.example.json` is a standalone fixed-port template for operators who
manage config files outside Compose. It contains no key material or caller
secrets.
