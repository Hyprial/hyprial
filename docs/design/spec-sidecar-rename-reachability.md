# Spec — the sidecar binary rename is unreachable on the branch its docstring names

- **Unit**: `sidecar-rename-reachability` · **Author**: infra-op · 2026-09-11
- **Base**: `origin/dev` at `c4d53fdb` (all line numbers below are read at that SHA)
- **Repo to change**: `HyprialOS/harness-bridge` — `src/hyprial/home_migration.py` and
  `tests/test_home_migration.py`
- **Found by**: hyprial-developer, 2026-09-11, reviewing #440's rename line
- **Status**: spec. ⛔ infra-op does not implement this.
- ⚠️ **Separate from `docs/design/design-sidecar-via-login.md` (#458)** on purpose: that unit changes
  the *install* flow and its reviewer is the install/consent surface; this one changes *home
  migration* and its reviewer is the rename line. One PR each.

---

## 0. The defect, as three separate claims

### 0.1 READING — the function is not called on the branch its docstring describes

```
home_migration.py:66  def _migrate_sidecar_binary_name(destination: Path) -> None:
              :67      """Finish the sidecar rename inside an already-migrated default home."""
                                                          ⭐ "already-migrated"
⇒ but the ONLY call is :174, and :174 sits AFTER :152's early return:
   :152  return LegacyHomeMigrationResult(False, "destination-exists", source, destination)
⇒ ⇒ 🔑 an already-migrated home (`~/.hyprial` exists) returns at :152 and never reaches :174
⇒ ⇒ ⇒ ⛔ so the function never runs in the exact situation its docstring names
```

### 0.2 ⛔ INVALID reachability story — do not use this one

The first framing was "machines that migrated before the rename code existed". That set is
**empty by construction**, and hyprial-developer verified it and withdrew it:

```
899b0fbc^1 (dev before #440) ⇒ home_migration file count = 0
899b0fbc   (after)           ⇒ 2
⭐ control: the same ls-tree for tsnet_sidecar.py on ^1 ⇒ 2   ⇒ 🔑 the command works; the 0 is real
git log -S'_migrate_sidecar_binary_name' 899b0fbc^1 ⇒ 0 commits
⇒ ⇒ 📌 home migration and the sidecar rename arrived in the SAME PR ⇒ there is no window
   between them ⇒ no machine can have taken one without the other
```

⚠️ This is written down rather than deleted because **looking for the wrong population is how
a real defect gets closed as unimportant**: search for "machines with a migrated home and an
old sidecar" and you find zero, and the obvious reading of that zero is "this does not matter".

### 0.3 ✅ VALID reachability — one failure, never retried

```
:165  os.rename(source, destination)                  ⇐ ⭐ the home is now migrated
:173  try:
:174      _migrate_sidecar_binary_name(destination)
:176  except OSError as error:
:179      raise _failure("sidecar-bin-rename-failed", …)
⇒ 🔑 "home migrated + sidecar still under the old name" is a state the code ITSELF declares,
   with its own reason code ⇒ the author expected it to happen
⇒ ⇒ ⛔ and on the next invocation :152 destination-exists returns early ⇒ :174 is never reached
⇒ ⇒ ⇒ ⭐ the code admits this step can fail, and provides no path back from the failure
```

⭐ This is the entry point that matters, and it is strictly weaker in its requirements than
0.2: 0.2 needs a historical window that never existed; 0.3 needs **one `OSError`**.

## 0.4 ⚠️ And the consequence is BOUNDED — write the bound, or this gets mis-prioritised

```
⭐ READING: `h2b-tsnet` appears 0 times in src/hyprial/tsnet_sidecar.py
⇒ 🔑 verify_installed_sidecar() looks only at bin/hyprial-tsnet ⇒ returns SIDECAR_MISSING
⇒ ⇒ ✅ so the machine still WORKS: login reports the sidecar as absent and offers to fetch it
```

The residue is three bounded things, not a broken install:

```
① bin/h2b-tsnet becomes a PERMANENT orphan file
   ⭐ and the function that would have removed it (:76 `legacy.unlink()`) is the very one
     that is never called again
② one redundant download on a machine that already had the binary
③ one misleading SIDECAR_MISSING on a machine where a sidecar is in fact present
```

⇒ 📌 So schedule this as *unfinished cleanup with a lying docstring*, ⛔ not as *machines are
broken*. ⚠️ "Bounded" does not mean "ignorable": the docstring promises something the code
does not do, which is the kind of defect that survives review by being believed.

## 0.5 ⭐ The part that is worse than the residue: it fails a daemon start

```
cli.py:3959  `hyprial daemon run` calls migrate_legacy_default_home() BEFORE
             require_initialized_hyprial_home()
⇒ 🔑 sidecar-bin-rename-failed therefore aborts daemon startup
⇒ ⇒ ⭐ and the NEXT start succeeds, because :152 returns early
⇒ ⇒ ⇒ ⚠️ so a real, diagnosable failure presents as a one-off flake that "cleared itself"
```

Other call sites, for completeness — ⛔ it is **not** only `hyprial init`:

```
(all at c4d53fdb; src/hyprial/cli.py is blob e4c3df09 there)
cli.py:3959   hyprial daemon run        ⇐ ⭐ THE one that matters — see below
cli.py:6258   _perform_upgrade
cli.py:7085   autoupdate run            ⇐ the 12-hourly timer
home.py:126   initialize_hyprial_home   ⇐ hyprial init only
```

⭐ `daemon run` is not merely the heaviest call site — it is **designed to be the one that
actually performs the migration**. Its own comment says so (`cli.py:3954-3956`):

> A renamed daemon launched by the **pre-rename upgrader** is the **first new-code process
> guaranteed to run** after the old daemon releases its databases. **Migrate before resolving
> state or socket paths.**

⇒ 🔑 So `sidecar-bin-rename-failed` lands on **the first daemon start after an upgrade** —
⇒ ⇒ ⚠️ which is the single moment most likely to be read as "the upgrade hiccupped", and
   §0.5's next-start-succeeds behaviour then confirms that misreading.

## 0.6 Where the reason code actually lands — ⛔ CORRECTED, the first version was false

⚠️ **This section said the wrong thing twice.** Both corrections are kept in place, because a
silently-replaced claim cannot be told apart from one nobody checked.

```
⛔ rev1: "search the daemon logs for the reason code"      ⇒ the daemon never calls this
⛔ rev2: "the code IS in the string — a 2-arg exception"   ⇒ ⭐ FALSE, found by codex-astra
```

### Why rev2 was false

```
:39  def __init__(self, message: str, data: dict[str, Any]) -> None:
:40      self.data = data
:41      super().__init__(message)      ⇐ ⭐ only the MESSAGE reaches args ⇒ len(args) == 1
⇒ 🔑 str(error) is the message alone. The reason never enters the string.
cli.py:906  the non-json branch prints str(error) ⇒ ⛔ no reason code on stderr
cli.py:865  _json_failure attaches error.data ⇒ ✅ the reason comes out ONLY under --json
```

### ⭐ And the split is exact — measured from the service templates

```
argv:
  daemon/services/systemd.service.template:8    ExecStart={{…}} daemon run      ⛔ no --json
  daemon/services/launchd.plist.template:10-11  daemon · run                    ⛔ no --json
  autoupdate/services/systemd.service.template:8   autoupdate run --json        ✅
  autoupdate/services/launchd.plist.template:12    --json                       ✅
stream capture — ⭐ ALL FOUR units capture BOTH streams (re-read with no truncation):
  daemon systemd     :20 StandardOutput=append   :21 StandardError=append
  daemon launchd     :38 StandardOutPath         :40 StandardErrorPath
  autoupdate systemd :12 StandardOutput=append   :13 StandardError=append
  autoupdate launchd :42 StandardOutPath         :44 StandardErrorPath
⇒ cli.py:894 writes --json to sys.stdout ⇒ data.reason lands in the STDOUT log
```

⇒ ⭐ **CORRECTED** — an earlier draft of this section said the index "is missing" on the
daemon side. ⛔ Too strong, and the true version is worse:

```
⭐ BOTH call sites persist something. ⛔ Only the LIGHTER one persists the DISCRIMINATING key.
  daemon run      ⇒ stderr IS written to a log ⇒ but only the drifting PROSE key
  autoupdate run  ⇒ stdout IS written to a log ⇒ ✅ data.reason, stable and discriminating
```

⇒ 🔑 That is worse than "unrecoverable", because **the site has something to find.** A person
greps the daemon log, gets hits, and concludes the search was complete — while the key they
used stops matching at the next wording edit, silently.

📌 So the finding is the **inversion**: the discriminating index sits on the call site that
barely matters, and the call site that actually performs the migration (§0.5) keeps only the
key that rots. ⛔ Not "the code is hard to find".

### ⚠️ Two further keys that look usable and are not

```
1 the class-level `code` (:37 HYPRIAL_HOME_MIGRATION_FAILED) is shared by ALL FIVE reasons
  this module can raise — rename-failed · sidecar-bin-rename-failed · daemon-state-unfenced
  · daemon-lock-unreadable · daemon-running
  ⇒ ⛔ so even under --json the `code` field does NOT discriminate. Only data.reason does.
2 stderr is not empty: :180 carries the prose
  "legacy HYPRIAL home migrated, but the sidecar binary rename failed: {error}"
  plus the raw OSError text
  ⇒ ⚠️ but that is a PROSE key. It drifts with any wording edit, and nothing fails when
    it drifts ⇒ 🔑 a grep built on it silently stops matching
```

⇒ 📌 Accurate statement of the situation: **the only stable, discriminating key is
`data.reason` under `--json`, and the call site that matters does not pass `--json`.**

⇒ ⭐ And the three conditions fail on **three different fields**, which is why the whole thing
reads as "there is a usable code":

```
(a) persisted      ✅ satisfied on BOTH sides (all four units capture both streams)
(b) discriminating ⛔ the class-level `code` is shared by 5 reasons; only data.reason qualifies
(c) reachable      ⛔ data.reason exists only under --json ⇒ unreachable on the daemon path
⇒ 🔑 no single field is obviously broken — each condition fails somewhere else
```

⚠️ The general shape, restated because this section is where it was got wrong: **a dedicated
error code indexes the affected set only if it is (a) persisted, (b) discriminating, and
(c) reachable on the path that actually fails.** Three conditions, and rev2 checked none of
them while sounding as if it had. ⛔ Making any of this recoverable is **out of scope** (§3);
it is written here so the fix is never justified by a query that cannot be run.

## 1. ⭐ Not a regression — an incomplete follow-up

```
git log -S'_migrate_sidecar_binary_name(destination)' -- src/hyprial/home_migration.py
⇒ c2f6bb48  fix(migration): 整目录迁移之后补一步 bin/h2b-tsnet → bin/hyprial-tsnet(Allen 裁 ii)
```

⇒ 🔑 The call was added *after* the directory migration, as a follow-up, and was placed only
on the path that had just done the directory rename. It was never on the other branch.
⇒ ⇒ ⭐ So the code is honest about what it does; the **docstring** is the over-claim, written
as if the step belonged to "an already-migrated home" in general.

📌 Therefore the commit message must say *completed an incomplete fix*, ⛔ not *fixed a
regression*: nobody removed anything.

## 2. What to change

### 2.1 Call the rename on the `destination-exists` branch as well

```
:151-:153   the early `destination-exists` return
⇒ call _migrate_sidecar_binary_name(destination) before returning it
⇒ ✅ safe because the function is ALREADY idempotent:
     :72  if not os.path.lexists(legacy): return          ⇐ old name absent ⇒ no-op
     :74  if os.path.lexists(current): legacy.unlink()     ⇐ both present ⇒ current wins
```

⭐ And the necessity argument, which matters more than the safety one: **that branch is the
only retry path `sidecar-bin-rename-failed` has.** Without it, nothing in the system can
clear that state. ⇒ 📌 "Safe" only gets a change accepted; "the sole retry path" is what gets
it scheduled.

⚠️ **`destination-exists` is returned twice** — at `:151` (guarded by `:150`, before the
fence) and at `:159` (guarded by `:158`, inside it as a TOCTOU re-check).

### 2.1a ⭐⭐ `:150` is LOAD-BEARING; `:158`'s reachability CONDITIONS are different

⇒ 📌 They are **not** two spellings of one branch. After the defect fires, `os.rename`
**succeeded**, so `source` is **gone** and `destination` exists:

```
next `daemon run`:
  :150  lexists(destination) ⇒ TRUE   ⇒ ⭐ returns at :151, right there
  to reach :158 you need :150 FALSE (destination absent) AND :154 FALSE (source present)
  ⇒ 🔑 which is the OPPOSITE state from the one the defect leaves behind
⇒ ⇒ ⭐ so the two branches have **different reachability conditions**, and the described
   failure residue hits the OUTER one directly — it does not pass the outer check and then
   land on the inner one. `:158` needs state or visibility to change **between two
   observations** (concurrent migration/creation), ⛔ not an ordinary retry.
```

⚠️ ⛔ **Word this carefully.** An earlier draft here said `:158` is "unreachable". codex-astra's
wording is better and is adopted: *the reachability conditions differ*, and he explicitly
**declines** to claim the inner branch is unreachable. ⇒ 🔑 "Unreachable" slides into "that
branch can be ignored"; "conditions differ" does not. 📌 Use his.

⇒ ⭐ **A fix placed only at `:158` would still do nothing for the scenario this spec targets.**
`:150` carries the repair; `:158` is defensive — ✅ harmless to add, ⛔ not sufficient alone.
📌 This is the answer to "which branch", and it is an *asymmetry*, not an enumeration.

#### ⚠️ Evidence layers — ⛔ do NOT flatten these

```
outer branch (:150)  ⇒ ✅ MEASURED — isolated probe, helper call count 0
inner branch (:158)  ⇒ ⚠️ control-flow review at a FIXED BLOB only
                       ⛔ NO TOCTOU concurrency timing was measured
two qualifiers that come with it:
  ① `source` existing at :154 does NOT imply it still exists at the re-check
  ② "entering the guard" ⛔ ≠ "acquired the flock" — with no lock and no state
     databases `_legacy_daemon_stopped` yields directly
```
⇒ 📌 Those qualifiers point at the residual uncertainty, and they are the reason the inner
branch gets a defensive call rather than a dismissal.

### ✅ And the acceptance set already pins the branch — ⛔ do NOT add a fourth case

⚠️ It was put to me that ①②③ would all stay green against a `:158`-only fix. ⛔ **They would
not**, and checking this matters because believing it would add a redundant test.

⛔⛔ **STANDING OF THIS PARAGRAPH: unreviewed.** It is *my reasoning plus hyprial-developer's
acceptance* — ⛔ **not** a review conclusion. codex-astra never saw it, deliberately: handing it
over would have turned his own reachability item from an independent reading into a
restatement. ⇒ 🔑 It is labelled rather than quietly presented because a correct-looking label
would occupy the slot for "has this been checked?". ⇒ ⭐ The place it gets genuinely checked is
the **implementation PR**, where ①②③ are real code and can simply be run against a
`:158`-only build.

```
① already-migrated home + legacy binary ⇒ :150 returns ⇒ helper never runs ⇒ not renamed
   ⇒ ✅ RED (its assertion is "it MUST be renamed")
②c after the failure, destination exists and source is gone ⇒ :150 returns
   ⇒ ✅ RED (asserts bin/hyprial-tsnet exists and bin/h2b-tsnet does not)
③ asserts the helper was called exactly once ⇒ a :158-only fix calls it 0 times
   ⇒ ✅ RED
```

⇒ 🔑 So ①②③ discriminate on **two axes at once**: swallow-vs-raise *and* branch placement.
📌 Worth stating, because that is not obvious from how they are written — and because the
next person's instinct on reading §2.1a will be to add a case for it.

### 2.1b ⛔⛔ The retry branch must SWALLOW `OSError` — a bare call makes this worse

The naive fix is a bare `_migrate_sidecar_binary_name(destination)` before the
`destination-exists` return. ⛔ **Do not write that.** Consider a *persistent* cause —
`~/.hyprial/bin/` owned by root after a sudo'd install, a read-only mount:

```
today        :174 raises once ⇒ then :152 returns early forever ⇒ ⭐ loud ONCE, then silent
bare call    every `hyprial daemon run` raises OSError out of migrate_legacy_default_home()
             ⇒ _execute's broad except ⇒ _fail ⇒ exit 1
⇒ ⇒ ⛔ §0.4's BOUNDED residue (an orphan file, machine works) becomes a PERMANENT
   daemon-start failure ⇒ 🔑 the fix would be strictly worse than the defect
```

⇒ ⭐ **Required shape:** the retry is best-effort.

```python
if os.path.lexists(destination):
    # Best-effort retry of the step that :174 may have failed at after the home was
    # already renamed. Swallowed deliberately: this branch is a RETRY, not the
    # migration. Raising here would turn a permanent cause (a root-owned bin/, a
    # read-only mount) into a permanent daemon-start failure, where today the
    # residue is one orphan file on a working machine.
    try:
        _migrate_sidecar_binary_name(destination)
    except OSError:
        pass
    return LegacyHomeMigrationResult(False, "destination-exists", source, destination)
```

⚠️ Written as `try/except` rather than `contextlib.suppress` on purpose: the module imports
`from contextlib import contextmanager` (`:15`) and **not** `contextlib` itself, so
`contextlib.suppress` would need an import change. Either spelling is fine — ⛔ just do not
let a `NameError` in review be mistaken for the design being wrong.

⭐ **The accepted cost, stated so it is a choice and not an omission.** Two distinct failure
forms, ⛔ not one:

```
① persistent RAISING cause (root-owned bin/, read-only mount)
   ⇒ the machine goes permanently SILENT — no code, no log, nobody learns
   ⇒ ✅ identical to today (:152 is already a silent early return) ⇒ not a new harm
② ⭐ persistent BLOCKING cause (a hung network mount)
   ⇒ ⛔ NOT silent — HUNG. `daemon run` stops there: no error, no progress
   ⇒ ⇒ 🔑 try/except catches a RAISE; it cannot catch a BLOCK
```

⚠️ ⛔ **CORRECTED 2026-09-11 — "it can never be worse than today" was FALSE.** codex-astra
falsified it by latching the helper's filesystem operation under controlled blocking: old code
returns early and never reaches it; the new snippet reaches it and does not return until the
latch is released.

⇒ ⭐ And the bound is sharper than "the new code has a blocking surface the old lacked" — the
surface is not new in **kind**, it is new in **frequency**:

```
helper's first statement is `os.path.lexists(legacy)` — a stat(), which on a hung mount
blocks uninterruptibly and raises nothing
today  reached ONLY at :174, on the one-time migrating path   ⇒ once per machine, ever
fixed  reached on the destination-exists branch               ⇒ ⭐ EVERY `daemon run`, forever
⇒ 📌 so the widening is once-ever → every-start. That is the actual cost.
```

⇒ ✅ Stated as a number rather than an adjective, which makes "is this acceptable" computable:

```
⭐ ONE extra stat() per daemon start, on every already-migrated machine, forever.
   On a healthy machine bin/h2b-tsnet does not exist, so the helper's first line returns
   immediately — the cost is that single stat and nothing more.
⛔ On a hung mount, that same stat blocks uninterruptibly and raises nothing.
```

✅ **Therefore the claim to write is the narrow one:** *only raising `OSError`s of the
described kind no longer escape.* ⛔ Do **not** write "never worse than today".

⛔ A timeout is **out of scope** and deliberately so: there is no portable way to bound
`os.stat`, and adding one would be a new mechanism rather than a completion of an incomplete
fix (§1). ⇒ 📌 It is named here so that choosing not to do it is visible as a choice. ⚠️ If
the every-start frequency is judged unacceptable, the right response is to reopen §2.1b's
decision, ⛔ not to bolt a timeout onto it.

⇒ 🔑 And note what ① and ② share: **both are invisible from the fix's own acceptance.** One
is silent, the other hangs — ⛔ neither produces a failing assertion.

⇒ 🔑 Why this and not "raise `sidecar-bin-rename-failed` every time": the loud-once behaviour
at `:174` is already correct — that is the path where the *home* moved and the caller must
know the migration was incomplete. `:152` is a different job. ⭐ It self-heals the moment the
cause clears, and it can never be worse than today.
⚠️ ⛔ And it must **not** also swallow at `:174`. Two branches, two policies, stated.

📌 This is the unit's **only** real design decision. ⇒ Whichever way it is resolved, the
reasoning goes in the diff as a comment — §2.2's rule applies here too.

### 2.2 ⭐ The daemon fence does NOT extend to this call — and the PR must say why

```
_legacy_daemon_stopped(source) (:82) fences **source** — it flocks
  source/state/daemon.lock and refuses on a live legacy daemon
⇒ but the destination-exists branch touches only **destination** (~/.hyprial/bin/)
⇒ ⇒ 🔑 and no live code references h2b-tsnet at all (0 occurrences in tsnet_sidecar.py)
⇒ ⇒ ⇒ ✅ so renaming or unlinking that file cannot race a running process
```

### 2.2b ⛔ CORRECTED 2026-09-11 — the first reason given here was false

The original text read: *"on that branch `source` may not exist, and the guard's
`daemon-state-unfenced` failure would then turn a currently-silent early return into a new
error class on machines that are fine."* ⛔ **That is wrong**, and codex-astra caught it
(independently re-read at blob `93d325a0`):

```
_legacy_daemon_stopped, :87-:101
  if not lock_path.exists():
      databases = [… for name in _STATE_DATABASES if (state / name).exists()]
      if databases: raise _failure("daemon-state-unfenced", …)   ⇐ ⭐ lock GONE but DBs PRESENT
      yield; return                                             ⇐ ⭐ both gone ⇒ plain no-op
⇒ 🔑 when `source` does not exist at all, lock_path.exists() is False AND databases is empty
⇒ ⇒ ✅ so the guard is a NO-OP and raises nothing. The hazard I named cannot happen.
```

⭐ **The hazard is the opposite precondition.** `destination-exists` says `~/.hyprial` exists;
it says nothing about `~/.h2b`, so **both can be present** — a still-installed legacy `h2b`
distribution that runs once recreates `~/.h2b` and its state databases. On *that* machine:

```
source present + a live legacy daemon          ⇒ the guard raises `daemon-running`
source present + state DBs but no daemon.lock  ⇒ the guard raises `daemon-state-unfenced`
⇒ ⛔ both for an operation that touches only destination/bin/ and never reads source
⇒ ⇒ 🔑 so wrapping converts a silent early return into an error — ⭐ but the trigger is
   `source` EXISTING, not `source` being absent
```

⇒ 📌 So the conclusion stands and the **stated reason was inverted**. ⚠️ That is worse than
having no reason: a wrong reason looks already-verified, so nobody re-checks it. ⇒ ⭐ The
reason that goes in the diff is the one above — the fence locks `source`, the call touches
`destination`, and on a machine where `source` happens to exist the fence would fail an
operation that has nothing to do with it.

⚠️ Write this reasoning into the diff, not only into the PR description — a PR description
does not travel with the code. A fence that is absent for a *reason* and a fence that is
absent by *oversight* look identical six months later, and so do a true reason and a false
one.

### 2.3 Fix the docstring to match whatever the code ends up doing

After 2.1 the docstring at `:67` becomes true. ⇒ If any branch is deliberately left out,
the docstring must name the exclusion instead of describing the general case.

## 3. ⛔ Out of scope

```
⛔ making sidecar-bin-rename-failed persistent (a log/receipt/state write).
   Real and worth its own unit (§0.6), ⛔ but it turns this change into a logging change.
⛔ teaching verify_installed_sidecar about the legacy name. That would mask §0.4's
   misleading SIDECAR_MISSING instead of removing its cause, and it re-introduces the old
   name into a module that currently has 0 references to it.
⛔ touching any of the other four early returns' semantics (explicit-home, source-absent,
   and the in-guard re-checks).
⛔ anything in the sidecar-via-login line (#458).
```

## 4. ⭐ Acceptance — two starting points, and ② is the one that is actually reachable

```
① already-migrated home + ONLY the legacy binary ⇒ it MUST be renamed
   ⇐ the clean, easy fixture
   ⚠️ and per §0.2 this starting state has no real-world producer — it is a STAND-IN for ②

② ⭐ the real path: make the rename fail ONCE, then succeed
   a. a legacy home with bin/h2b-tsnet, no ~/.hyprial
   b. inject a single OSError into _migrate_sidecar_binary_name
      ⇒ assert the raise carries reason == "sidecar-bin-rename-failed"
      ⇒ assert ~/.hyprial EXISTS (the home did migrate) and bin/h2b-tsnet is still there
   c. run migrate_legacy_default_home() again, with no injection
      ⇒ assert bin/hyprial-tsnet exists and bin/h2b-tsnet does not
   🔑 ② is the only starting point a real machine can reach, and its start state is a
     HALF-FINISHED one — which only a failure can produce
   ⇒ ⭐ a fixture that starts from "a clean legacy home" can never reach it. That was the
     blind spot in ① as originally proposed.

③ ⭐ PERSISTENT failure ⇒ the retry must stay QUIET, and the result must stay
   `destination-exists`
   a. reach the state from ② step b (home migrated, legacy binary present)
   b. keep the injection in place — the cause does NOT clear
   c. run migrate_legacy_default_home() again
      ⇒ assert it RETURNS (reason == "destination-exists"), ⛔ does not raise
      ⇒ assert bin/h2b-tsnet is still there (nothing was repaired, and nothing broke)
      ⇒ ⭐ assert the helper was CALLED EXACTLY ONCE  ⇐ 🔑 the only POSITIVE assertion here

   ⛔⛔ **CORRECTED 2026-09-11 — as first written, this case was a VACUOUS control.** Its
   three original assertions were all NEGATIVE (does not raise · still returns
   destination-exists · file still present), and codex-astra ran it against the
   **unmodified product baseline**: all three pass, because a baseline that never calls the
   helper cannot raise, returns destination-exists at :152 by construction, and touches no
   file. ⇒ 🔑 **A purely negative assertion set is satisfied by doing nothing.**

   ⇒ ⭐ So what ③ actually discriminates, stated exactly:
```
helper call count 1 vs 0   ⇒ separates {either fixed implementation} from {baseline}
raise vs no raise          ⇒ separates {try/except} from {bare call}
⇒ 📌 BOTH assertions are required; either alone leaves a pair indistinguishable
```
   ⛔ **③ is an ANTI-REGRESSION control. It does NOT on its own prove the retry happened**,
   and ⛔ it must not be read as "the fix is in effect". ⚠️ Write that in the test's docstring:
   the next reader will otherwise cite ③'s green as evidence of a working repair.

   ⚠️ And the criterion I accepted for this case — "run it against a bare call and require
   RED" — was **itself** incomplete, for the same reason: it supplied one wrong implementation
   as the control and omitted the **empty** one. ⇒ ⭐ A test's control set needs *both* a
   wrong implementation **and** no implementation. 📌 The baseline is the harder one to
   remember, because it is the thing you are trying to improve rather than a mistake you
   imagined.

④ both-names-present ⇒ the current name wins, the legacy file is removed
   ⇐ this is what a machine looks like AFTER §0.4's redundant download, so it is reachable
     even without the fix. ⚠️ tests/test_home_migration.py:99 already covers the mechanism
     on the other branch; it must now hold on this branch too.

⛔ How NOT to inject the failure: `chmod` on bin/. CI that runs as root ignores the mode,
  the injected failure never happens, and ② passes VACUOUSLY while testing nothing.
✅ Inject it the way this file already does it — `patch.object(home_migration,
  "_migrate_sidecar_binary_name", …)`; see :65, :88, :111 for the existing `wraps=` idiom.
  Raise on the first call, delegate on the second.

⭐ CONTROL, required: each new test MUST FAIL on c4d53fdb before the fix, and the PR must
  show that run. A test that passes both before and after is measuring something else.

⚠️⛔ **TEST UNIT and CRITERION are different granularities — found by codex-astra, and the
text above previously conflated them.**

```
"each new test MUST FAIL on c4d53fdb"  ⇒ a statement about a TEST UNIT
"③ is green on the baseline"           ⇒ a statement about one CRITERION inside a unit
⇒ 🔑 if ①②③ live in ONE test function, that unit can be RED because of ② while ③'s own
   assertions are green on the baseline
⇒ ⇒ ✅ so "③ green on baseline" does NOT conflict with the MUST-FAIL rule at all
⇒ ⇒ ⇒ ⛔ the conflict exists only in a reading that treats the two as one granularity
```
⇒ 📌 So: ③ must be read **together with** ②, and the MUST-FAIL rule must ⛔ **not** be
re-expressed as "③ alone must be red on the baseline". ⭐ Keep the distinction explicit
wherever either statement appears — ⚠️ hyprial-developer and I spent a whole exchange arguing
about what "③ green on baseline" meant, and the answer was that the question was malformed.

⭐⭐ REQUIRED SELF-CHECK before trusting any case here — fill in this table, by hand:
```
                        │ baseline  │ bare call │ try/except │ :158-only
                        │ (no impl) │ (wrong)   │ (intended) │ (wrong branch)
  each assertion of ①   │           │           │            │
  each assertion of ②   │           │           │            │
  each assertion of ③   │           │           │            │
⇒ 🔑 every row must distinguish the INTENDED column from EVERY other column
⇒ ⇒ ⛔ a row that reads the same across two columns is not a check, it is a decoration
```
⚠️ This table exists because the acceptance set was got wrong **twice in one day, in opposite
directions**, and both times by reasoning about *which code path runs* instead of about *what
the assertions read*:

```
⛔ "these negative assertions will catch a missing implementation"  ⇒ they all pass on baseline
⛔ "these assertions cannot catch a wrong-branch implementation"    ⇒ all three go red
⇒ 🔑 common cause: path reasoning cannot tell "did not run" from "ran and changed nothing",
   ⭐ while an assertion reads only the END STATE — where those two are identical or not,
   depending on the assertion, which is exactly what the table makes visible
```
⇒ 📌 So the rule is: **drive each candidate implementation through the assertions, not through
the control flow.** ⛔ Columns are cheap; a wrong column costs a redundant test or a missing one.
  ⚠️ Today tests/test_home_migration.py has 5 tests and NONE reaches the failure path:
  grep `sidecar-bin-rename-failed` in tests/ ⇒ 0 hits.
```

## 5. Where this is measured from

Every line number and count above was read at `origin/dev` `c4d53fdb` on 2026-09-11, with
`git show origin/dev:<path>`. ⇒ ⭐ Re-read before implementing: `c4d53fdb` is dev's tip today,
⛔ not a tag, so it moves. The four structural facts the spec depends on are:

```
1 _migrate_sidecar_binary_name has exactly ONE call site
2 destination-exists is returned at two places, one outside and one inside the guard
3 h2b-tsnet occurs 0 times in src/hyprial/tsnet_sidecar.py
4 sidecar-bin-rename-failed occurs 0 times in tests/
```

⇒ 📌 If any of the four has changed, this spec needs re-reading, ⛔ not patching.

### ⚠️ How to re-verify a line number across two checkouts

A reviewer reading these numbers from a different tree reported every `cli.py` number as
**+25**, and explained it as "dev moved between us". ⛔ It had not:

```
git rev-parse 899b0fbc:src/hyprial/cli.py  ⇒ e4c3df09…
git rev-parse c4d53fdb:src/hyprial/cli.py  ⇒ e4c3df09…   ⭐ the SAME blob
git diff --stat 899b0fbc c4d53fdb -- src/hyprial/cli.py ⇒ empty
⇒ 🔑 the file is byte-identical at both commits, so a +25 offset cannot come from the commit
⇒ ⇒ ⛔ and no remote ref produces 3984 either (scanned) ⇒ the reading came from an
   uncommitted working tree, not from a different revision
```

⇒ ⭐ **Verify the blob, not the commit.** Two people on different commits of an unchanged
file are looking at the same bytes, and a "the branch moved" story explains a discrepancy
that has a different cause — which then hides the real one. ⇒ 📌 Read line numbers with
`git show <sha>:<path>`, ⛔ not from a checkout, and quote the blob hash when they are
disputed.
