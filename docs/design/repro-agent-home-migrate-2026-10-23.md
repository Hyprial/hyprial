# Reproducing the agent-home migration checks

Reviewer's companion to `evidence-agent-home-migrate-2026-10-23.md` (which is a
byte-identical copy of the executor's `EVIDENCE.md`). This file exists because
the evidence quoted commands whose interpreter was implicit, and a reader who
runs `python3 -m pytest` gets an import error that looks like a failing suite.

Run these from the repository root. Every command names its interpreter; none
depends on `PATH`, an active virtualenv, or a shell alias.

## Readings on this machine

Observed at `epoch 1792744692` in a worktree whose remote `dev` is
`ad5ae260be2326fa8e87e0a89ec3b47bebbf4ce3`:

```sh
# 1. focused suite -- 20 tests
$ ./.venv/bin/python -m pytest -q tests/scripts
20 passed, 2 warnings in 0.11s

# 2. lint
$ ./.venv/bin/python -m ruff check scripts tests/scripts
All checks passed!

# 3. structural gates
$ ./.venv/bin/python scripts/ci/check_structural_limits.py
  "status": "PASS", "violations": []
$ ./.venv/bin/python scripts/ci/check_module_boundaries.py
  "status": "PASS", "violations": []
```

The two warnings come from `lark_oapi` (`asyncio.get_event_loop` deprecation),
not from these scripts.

## The command that does not work, and what it means

```sh
$ python3 -m pytest -q tests/scripts
/Library/Developer/CommandLineTools/usr/bin/python3: No module named pytest
```

This is **NOT-RUN**, not FAILED. The interpreter has no pytest installed; no
test executed, none failed. Two different states that look similar in a
terminal:

```text
NOT-RUN   the command could not start          -> says nothing about the code
FAILED    the command started and reported red -> says something about the code
```

A reader who treats NOT-RUN as red will reject correct work; a reader who treats
FAILED as NOT-RUN will merge broken work. State which one you saw.

## Negative controls

Each control disables exactly one protection and shows the test that guards it
turning red. Skipping this step makes the test a document rather than a check:
a test that passes both with and without the bug measures nothing.

| Disable | Observed (`epoch 1792744523`) |
|---|---|
| `workspace/records` entry removed from `MOVES` | run returns ok, source still present, destination not created |
| `_reject_git_pointer` neutered | pointer moved verbatim, still aiming at the old `gitdir` |
| `EXCLUSIVE_SOURCES` emptied | apply reaches `ENOTEMPTY` and rolls back, after writes start |

The third row corrects an earlier claim of mine: the ambiguity guard does not
prevent data loss (the rollback does). It fails **before any write**, so the
report does not depend on the rollback path working, and it names both paths
instead of surfacing an errno.

## What is still not run

`pytest tests/` (the full suite) was **not** run to completion: the executor
stopped at 31% with exit 130 on failures in modules that neither import nor
exercise these scripts. No pass/fail count is claimed for it.

## Cross-device, measured on a real second filesystem

An earlier commit message said a real cross-device rename could not be measured
here. That was wrong: a small APFS image supplies the second device.

```sh
$ hdiutil create -size 50m -fs APFS -volname xdevtest -quiet /private/tmp/xdev.dmg
$ mkdir -p /private/tmp/xdevmnt
$ hdiutil attach -mountpoint /private/tmp/xdevmnt -quiet /private/tmp/xdev.dmg
$ stat -f %d /private/tmp /private/tmp/xdevmnt
16777231
16777250                     # two device ids: the second filesystem is real
                             # ids change on every mount -- never assert a literal
```

Raw rename from one device to the other:

```sh
$ ./.venv/bin/python -c 'import os; os.rename("/private/tmp/xdevmnt/p.txt", "/private/tmp/x.txt")'
OSError errno=18 Cross-device link
```

The shape that matters -- a home that is itself a mount point, so the staging
directory would land on the other device:

```sh
$ ./.venv/bin/python scripts/agent-home-migrate.py /private/tmp/xdevmnt --yes
{"ok": false, "error": "cross-device migration is unsupported: /private/tmp/xdevmnt/records
 (device 16777250) stages under /private/tmp (device 16777231); os.rename cannot copy
 across filesystems"}
$ echo $?
2
$ ls /private/tmp/xdevmnt
records                      # untouched; workspace/notes was never created
$ hdiutil detach /private/tmp/xdevmnt -quiet
```

Readings at `epoch 1792745290`.

**Two different injections, two different claims.** The device check firing is
*prevention*: it runs in `_plan`, and the destination is asserted absent.
Injecting `EXDEV` from `os.rename` is *reaction*: it covers the case where the
check is fooled (bind mount, race) and the transaction must roll back. The
reaction test would pass for a refusal that happens after the first write, so it
cannot stand in for the prevention test.

## Why this stays a manual procedure, not a CI test

The portable cross-device coverage is the injected-device check, which runs
everywhere. The `hdiutil` procedure above is macOS-only, so it stays a
documented manual reading rather than a test: a test that silently returns when
`hdiutil` is missing would report the same green line as a test that ran, and
that is the same defect as an unaccounted skip. If it is ever automated, the
missing-tool path must report `SKIP{no-hdiutil}` and be counted, never `pass`.

## Provenance of the manual reading

The device ids and the `hdiutil` output above were observed on one machine at
one time; both change with the host and the mount.

| field | value |
|---|---|
| observed_at | 2026-10-23T08:59:02Z |
| host | BoBADmin的Mac Studio |
| macOS | 26.5.2 |
| epoch | 1792745290 |
| volumes | disk3s5 (/System/Volumes/Data) and an APFS image attached at /private/tmp/xdevmnt |

A reading without these three fields cannot be reproduced, only quoted.
