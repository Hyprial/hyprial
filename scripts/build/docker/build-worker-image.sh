#!/bin/sh
# Build the hyprial worker image and tag it hyprial-worker:<hyprial version>
# (decision D-C: the image ships to transfer targets as a `docker save`
# tar through the existing transfer transport; no registry).
set -eu

cd "$(dirname "$0")/../../.."

uv build --wheel
version="$(uv run python -c 'from hyprial import __version__; print(__version__)')"
tag="hyprial-worker:$(printf '%s' "$version" | tr '+' '-')"

# The build sandbox's loopback is NOT the host. Docker Desktop resolves
# host.docker.internal; other builders must provide an equivalent host mapping
# (or unset the proxy variables). Preserve scheme, userinfo, port and suffix;
# never rewrite a lookalike domain or loopback text in a username/path.
rewrite_loopback_proxy() {
  case "$1" in
    *://*) scheme="${1%%://*}://"; rest="${1#*://}" ;;
    *) scheme=""; rest="$1" ;;
  esac
  authority="${rest%%[/?#]*}"
  suffix="${rest#"$authority"}"
  case "$authority" in
    *@*) userinfo="${authority%@*}@"; host_port="${authority##*@}" ;;
    *) userinfo=""; host_port="$authority" ;;
  esac
  case "$host_port" in
    localhost|127.0.0.1|'[::1]') host_port="host.docker.internal" ;;
    localhost:*|127.0.0.1:*|'[::1]:'*) host_port="host.docker.internal:${host_port##*:}" ;;
  esac
  printf '%s%s%s%s' "$scheme" "$userinfo" "$host_port" "$suffix"
}

# BuildKit forwards these to RUN steps; uv/npm/pip downloads need the
# host's proxy inside the build sandbox. Quoted positional args preserve
# spaces/globs in URLs and NO_PROXY; bypass lists must NOT be rewritten.
set --
for var in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
  eval "value=\${$var:-}"
  if [ -n "$value" ]; then
    case "$var" in
      NO_PROXY|no_proxy) ;;
      *) value="$(rewrite_loopback_proxy "$value")" ;;
    esac
    set -- "$@" --build-arg "$var=$value"
  fi
done

docker build "$@" -f scripts/build/docker/Dockerfile.hyprial-worker -t "$tag" .
echo "built $tag"
