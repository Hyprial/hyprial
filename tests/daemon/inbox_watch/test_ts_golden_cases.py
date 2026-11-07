"""Drive the TypeScript inbox-watch port through the shared golden cases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

CASES = Path(__file__).resolve().parents[3] / "contract" / "inbox-watch" / "cases"
TS_CORE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "hyprial"
    / "daemon"
    / "impl"
    / "harnesses"
    / "pi"
    / "resources"
    / "pi_inbox_watch.ts"
)
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

DRIVER = r"""
import fs from "node:fs";
import {
  InboxWatch,
  Ok,
  Rejected,
  Unreachable,
} from "./pi_inbox_watch.ts";

const document = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const config = document.config ?? {};
const watch = new InboxWatch({
  pollIntervalMs: config.pollIntervalMs ?? 500,
  heartbeatIntervalMs: config.heartbeatIntervalMs ?? 1000,
  holdMs: config.holdMs ?? null,
  useWait: config.useWait ?? true,
});
let now = 0;
const outstanding = {};

function actionJson(action) {
  const result = { op: action.op };
  if (action.op === "wait") {
    result.knownMessageIds = [...action.knownMessageIds];
    if (action.holdMs !== null) result.holdMs = action.holdMs;
  } else if (action.op === "sleep") {
    result.ms = action.ms;
  }
  return result;
}

function outcome(respond) {
  if ("ok" in respond) return new Ok(respond.ok);
  if ("rejected" in respond) return new Rejected(respond.rejected);
  if (respond.unreachable === true) return new Unreachable();
  throw new Error("unknown respond shape " + JSON.stringify(respond));
}

function changeJson(change) {
  if (change === null || change.empty) return {};
  const result = {};
  if (change.newMessages.length > 0) {
    result.new = change.newMessages.map((row) => row.messageId);
  }
  if (change.rewake.length > 0) {
    result.rewake = change.rewake.map((row) => row.messageId);
    result.rewakeReason = change.rewakeReason;
  }
  if (change.completed.length > 0) result.completed = [...change.completed];
  return result;
}

function equal(actual, expected, where) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  if (left !== right) {
    throw new Error(`${where}: expected ${right}, got ${left}`);
  }
}

for (const [index, step] of document.steps.entries()) {
  const where = `${document.id} step ${index}: ${JSON.stringify(step)}`;
  const at = step.at ?? now;
  if (at < now) throw new Error(`${where}: time went backwards`);
  now = at;
  const lane = step.lane;
  if ("rewake" in step) {
    watch.requestRewake(step.rewake, step.now ?? true);
    continue;
  }
  if ("observe" in step) {
    watch.observe(step.observe);
    continue;
  }
  if ("useWait" in step) {
    watch.useWait = step.useWait;
    continue;
  }
  if ("observeRejected" in step) {
    watch.observeRejection(step.observeRejected);
    continue;
  }
  if ("expect" in step) {
    const action =
      lane === "poll" ? watch.pollNext(now) : watch.heartbeatNext(now);
    equal(actionJson(action), step.expect, where);
    if (action.op !== "sleep" && action.op !== "quiet") {
      outstanding[lane] = action;
    }
    continue;
  }
  const action = outstanding[lane];
  delete outstanding[lane];
  if (action === undefined) throw new Error(`${where}: no outstanding action`);
  const result = outcome(step.respond);
  const change =
    lane === "poll"
      ? watch.pollDone(action, result, now)
      : (watch.heartbeatDone(action, result, now), null);
  equal(changeJson(change), step.emit ?? {}, where);
}

for (const [name, expected] of Object.entries(document.counters ?? {})) {
  equal(watch.counters[name] ?? 0, expected, `${document.id}: counter ${name}`);
}
process.stdout.write(JSON.stringify({ ok: true, counters: watch.counters }) + "\n");
"""


def _cases() -> list[Path]:
    return sorted(CASES.glob("*.json"))


@pytest.mark.parametrize("case_path", _cases(), ids=lambda path: path.stem)
def test_typescript_golden_case(case_path: Path, tmp_path: Path) -> None:
    shutil.copy(TS_CORE, tmp_path / TS_CORE.name)
    (tmp_path / "driver.ts").write_text(DRIVER, encoding="utf-8")
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
    }
    completed = subprocess.run(
        [NODE, "driver.ts", str(case_path)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["ok"] is True
