# Four Lark adapters added through the current CLI recover online

## Preconditions

- The editable or installed `hyprial` executable contains the Python daemon and adapter CLI.
- The runner owns disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR` roots under a
  short writable platform temp root (`/private/tmp` on macOS or `/tmp` on Linux).
- The isolated worker simulates only the Lark WebSocket ready boundary; no production credential or network is used.

## Steps

1. Create a disposable Python home and add four Lark adapters named for their role (`e2e007-fake-lark-1` … `e2e007-fake-lark-4`) through `hyprial adapter add`, using disposable test credentials. They run on a fake worker and send no Feishu request, so they do not borrow production bot names.
2. Start the Python daemon and invoke `hyprial adapter start <name>` for all four adapters.
3. Assert `hyprial daemon status` reports all four adapters `online` and desired state records each as `provider=lark`.
4. Stop and restart the daemon, then assert all four desired adapters return `online`.

## Assertions

- Public adapter registration succeeds for all four names and never prints a secret.
- The daemon loads exactly the CLI-produced adapter configuration.
- `e2e007-fake-lark-1` through `e2e007-fake-lark-4` are simultaneously online.
- Desired state records exactly the four `lark` providers.
- Daemon restart restores all four from desired state.
- The production home and daemon are never accessed.

Run:

```sh
uv run python contract/e2e/run-four-lark-adapters.py
```
