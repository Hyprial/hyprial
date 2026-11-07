#!/bin/sh
# hyprial worker container entrypoint (docs/design/design-transfer-container.md §2).
#
# 1. Populate the tmpfs HOME from the read-only credential volume: the
#    runtime copy lives only in container memory-backed storage and never
#    touches a host path ("容器停止后凭据不残留宿主", D-A).
# 2. Fix ownership (bind-mount parents docker creates are root-owned).
# 3. Drop to the daemon's uid and exec the harness command.
set -eu

HOME_DIR="${HOME:-/home/hyprial}"

if [ -d /hyprial-cred ]; then
  (cd /hyprial-cred && find . -type f) | while read -r f; do
    rel="${f#./}"
    dest="$HOME_DIR/$rel"
    mkdir -p "$(dirname "$dest")"
    cp "/hyprial-cred/$rel" "$dest"
    chmod 600 "$dest"
  done
fi

uid="${HYPRIAL_CONTAINER_UID:-}"
gid="${HYPRIAL_CONTAINER_GID:-}"
if [ -n "$uid" ] && [ -n "$gid" ]; then
  chown -R "$uid:$gid" "$HOME_DIR" 2>/dev/null || true
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi
exec "$@"
