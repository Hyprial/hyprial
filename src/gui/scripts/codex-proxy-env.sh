# Sourced only by the GUI's DSH runtime launcher, after .env.local is loaded.
# Package installation retains its own proxy environment for registry downloads.
export DSH_CODEX_PROXY="${DSH_CODEX_PROXY:-${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-${all_proxy:-${ALL_PROXY:-}}}}}}}"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy
unset NODE_USE_ENV_PROXY
