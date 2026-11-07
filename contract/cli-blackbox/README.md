# CLI black-box contract

This suite runs against an installed `hyprial` executable and asserts only public
JSON output and exit semantics. It has no Node or Python package dependencies.

## Safety boundary

- `help`, `version`, `log`, `send`, `ack`, `logout`, and the no-daemon discovery
  checks run with fresh temporary `HYPRIAL_HOME`, `HARNESS_STATE_DIR`, and npm
  cache paths.
- `send` and `ack` are deliberately exercised only against the isolated,
  non-running daemon. Their contract is an explicit `DAEMON_UNAVAILABLE`
  failure before any delivery or acknowledgement can occur.
- The default suite also runs ambient `ps`, `doctor`, and `targets`. These are
  read-only observations of the currently installed TS daemon and network.
- The suite never calls `init`, `start`, `attach`, `down`, `upgrade`, track
  mutation, channel mutation, agent mutation, or inbox pruning. Its logout
  contract creates only synthetic user-login, device-key, and agent-credential
  sentinels in the temporary home, then proves logout removes only the login
  state. It never copies production credentials or daemon state into its fixture.
- Output reports only check names, never connector records, target records,
  doctor details, log bodies, or credential paths.

## Run

```sh
./contract/cli-blackbox/run.sh
```

The wrapper uses the same interpreter order as `contract/run-all.sh`:
`HYPRIAL_PYTHON`, repository `.venv`, non-empty `VIRTUAL_ENV`, then `python3`. This
standalone dependency-free corpus requires Python >=3.12 but does not require
the hyprial project dependencies, so it remains usable against the TS reference.
If the `cli-blackbox/` directory is copied without the repository resolver, its
wrapper retains the portable `HYPRIAL_PYTHON`-or-`python3` fallback and performs
the same strict file, executable, identity, and version validation.

Select another implementation without changing the suite:

```sh
./contract/cli-blackbox/run.sh --hyprial /path/to/python/hyprial
```

CLI selection is `--hyprial`, then `HYPRIAL_BIN`, then `hyprial` on PATH. This standalone
entry point intentionally does not select the repository `.venv/bin/hyprial`;
the aggregate `contract/run-all.sh` adds that repository default before PATH.

The sealed TS reference at `dev@71ea88c` has the versioned doctor schema. Its
strict gate is:

```sh
./contract/cli-blackbox/run.sh --hyprial /path/to/71ea88c/dist/cli.js --doctor-schema sealed
```

`--doctor-schema auto` is the default because the globally installed 0.3.3
binary can predate the final source build. Auto mode fully validates whichever
known schema is emitted; it does not weaken the selected schema's assertions.
The Python equivalence gate should use `--doctor-schema sealed`.

For hermetic CI without a live reference daemon or network:

```sh
./contract/cli-blackbox/run.sh --isolated-only
```

The live checks are environmental schema checks: `doctor.ok` may be either
true or false. In sealed schema v1 it means zero `fail` rows and every non-ok
row must carry an executable action; in the legacy schema it equals the
conjunction of check results. This keeps registry or network outages from being
mistaken for a JSON contract regression.

Every command's exit status agrees with `ok`: `ok:false` exits 1 (doctor included,
so a failing check row makes `doctor --json` exit 1), argument errors exit 2,
and success exits 0.

Interactive Codex credential failures expose the typed remediation envelope in
[`codex-credential-preflight.md`](codex-credential-preflight.md).
