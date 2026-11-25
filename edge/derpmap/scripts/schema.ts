import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const merged = await import("../src/index.ts");
writeFileSync(
  "test/schema/merged.json",
  `${merged.stableStringify(merged.mergeDerpMap(null))}\n`,
);
const go =
  process.env.DERPMAP_GO ??
  join(homedir(), "go/pkg/mod/golang.org/toolchain@v0.0.1-go1.27.1.darwin-arm64/bin/go");
execFileSync(go, ["test", "./..."], {
  cwd: "test/schema",
  stdio: "inherit",
  env: {
    ...process.env,
    GOTOOLCHAIN: "local",
    GOPROXY: "off",
    GOSUMDB: "off",
    GOMODCACHE: process.env.GOMODCACHE ?? join(homedir(), "go/pkg/mod"),
  },
});
