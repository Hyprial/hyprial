#!/usr/bin/env bash
# Run inside a normal optional step: remote `uses:` downloads happen before
# any step-level condition, timeout or continue-on-error can protect the job.
set -euo pipefail
cache_tool_tmp=$(mktemp -d)
trap 'rm -rf "$cache_tool_tmp"' EXIT
cache_tool_commit=0057852bfaa89a56745cba8c7296529d2fc39830
curl --fail --silent --show-error --location --http1.1 \
  --connect-timeout 15 --max-time 60 --retry 1 --retry-delay 2 \
  "https://codeload.github.com/actions/cache/tar.gz/${cache_tool_commit}" \
  --output "$cache_tool_tmp/action.tar.gz"
tar -xzf "$cache_tool_tmp/action.tar.gz" -C "$cache_tool_tmp" --strip-components=1
(
  cd "$cache_tool_tmp"
  # Digests from the pinned upstream commit, independent of archive metadata.
  # Verified with Node's crypto rather than sha256sum: the CLI's operand
  # conventions differ across platforms.  Apple's sha256sum requires a file
  # operand, so `--check` reading a heredoc exits with a usage error and
  # verifies nothing -- a security check that silently stops checking.  Node
  # is already a hard dependency of the steps that run before this one.
  node -e '
const { createHash } = require("node:crypto");
const { readFileSync } = require("node:fs");
const expected = [
  ["dist/restore-only/index.js", "f740b8f77a9eeb0223d54b55e14df240e7ad6338e9648730d8e4551cf78ae172"],
  ["dist/save-only/index.js", "7f64677407a4befe523854fb28794faa4e2d344dfed5d6cd4887fa9ae5428b3f"],
];
let failed = 0;
for (const [file, want] of expected) {
  const got = createHash("sha256").update(readFileSync(file)).digest("hex");
  if (got === want) { console.log(file + ": OK"); continue; }
  console.error(file + ": FAILED");
  failed += 1;
}
if (failed > 0) {
  console.error("sha256 check: " + failed + " computed checksum(s) did NOT match");
  process.exit(1);
}
'
)
mkdir -p .ci-cache/cache-action
cp "$cache_tool_tmp/dist/restore-only/index.js" .ci-cache/cache-action/restore.cjs
cp "$cache_tool_tmp/dist/save-only/index.js" .ci-cache/cache-action/save.cjs
printf 'ready=true\n' >> "$GITHUB_OUTPUT"
