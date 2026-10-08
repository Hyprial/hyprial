"""Transfer runtime-intent commands (``hyprial transfer-runtime``)."""
from __future__ import annotations

from pathlib import Path

import typer



def register_runtime_commands(transfer_runtime_app, get_dependencies):

    @transfer_runtime_app.command("downgrade-state")
    def transfer_runtime_downgrade_state(
        output: Path = typer.Option(..., "--output", help="New directory for durable runtime-intent export."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Offline rollback preparation: retire smolvm intent before installing an older daemon."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _state_dir = dependencies.state_dir
        from hyprial.daemon import downgrade_state
        _execute(lambda: downgrade_state(_state_dir(), output), json_output=json_output)


    @transfer_runtime_app.command("probe")
    def transfer_runtime_probe(
        smolvm: Path = typer.Option(..., "--smolvm", help="Absolute path to a local smolvm 1.19.0 executable."),
        rootfs: Path = typer.Option(..., "--rootfs", help="Absolute path to an unpacked local Linux rootfs; never downloaded."),
        resize2fs: Path = typer.Option(..., "--resize2fs", help="Absolute path to host resize2fs, required for the fixed 1 GiB disk."),
        output: Path = typer.Option(..., "--output", help="New private evidence directory; its parent must already exist."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Run a synthetic, isolated VM mapping probe (no worker or credential test)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        from hyprial.daemon import probe_smolvm

        _execute(lambda: probe_smolvm(binary=smolvm, rootfs=rootfs, output=output, resize2fs=resize2fs),
                 json_output=json_output, allow_missing_home=True)

    return {
        "transfer_runtime_downgrade_state": transfer_runtime_downgrade_state,
        "transfer_runtime_probe": transfer_runtime_probe,
    }
