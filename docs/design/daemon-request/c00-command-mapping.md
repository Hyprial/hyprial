# C00 command-to-daemon mapping

This document describes the current C00 projection. The machine-readable mapping is [c00-command-mapping.json](c00-command-mapping.json), generated from the committed `docs/reference/cli-command-inventory.json` and the registered Typer command tree.

The current inventory contains 218 executable entries, with 214 visible entries, 4 hidden entries, 214 leaves, and 4 executable groups. It has 23 source families. Its SHA-256 is `e1ab379debd02ac9baae22b07cad19fd031d9d9e9e92446ec784aeb19fee9d41`.

The mapping contains one entry for every current executable inventory path. Callback evidence is resolved from the current registered callbacks with `inspect.unwrap`, recording module, symbol, and source line for all 218 entries. Each source evidence reference uses the current mapping-generation commit. Dependencies are current annotations. Retired `agent host-invite` and `agent link` CLI paths are absent. Entries without a prior semantic annotation carry `UNKNOWN` and an empty dependency list rather than inheriting a retired dependency.

Capability counts are many-to-many entry memberships. The current mapping records 31 entries as `UNKNOWN`, 14 as bootstrap exemptions, 59 as excluded, 86 as legacy IPC, 22 as local business, 5 as method-spec, and 2 as presentation. The daemon method catalog remains a separate catalog of retained daemon methods and does not add CLI entries.

The `generatedFrom.refs` values in the JSON are historical C00 snapshot references. They identify the source snapshots used when the original semantic annotations were recorded and are not current inventory counts or current callback evidence.

## Verification

The official inventory generator and validation command is:

```text
UV_CACHE_DIR=/tmp/uv-cache uv run --no-project --python 3.12 python scripts/verify/check_cli_help_inventory.py --write
PASS command tree inventory match: 266 nodes (214 leaves, 52 groups, 4 executable groups; 218 executable entries, 6 hidden nodes (3 direct))
PASS Typer/help top-level inventory match: 20 commands (20 visible, 0 hidden)
```

The C00 consistency check confirms that the mapping paths equal the inventory executable paths, the inventory SHA matches, all callback evidence is present, and no retired `agent host-invite` or `agent link` path remains.
