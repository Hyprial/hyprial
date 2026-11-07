"""The shell bootstrap is the one owner of the post-install module warmup."""

from __future__ import annotations

from hyprial.shell import prime_post_install_imports
from hyprial.shell.impl.cli.commands.socialware.commands import (
    _prime_post_install_restart_imports,
)


def test_cli_preload_is_the_public_shell_capability() -> None:
    assert _prime_post_install_restart_imports() == prime_post_install_imports()


def test_shell_bootstrap_loads_the_named_daemon_and_identity_modules() -> None:
    primed = prime_post_install_imports()
    assert "hyprial.daemon.impl.harnesses.model_provider" in primed
    assert "hyprial.identity.impl.agents.runtime" in primed
    assert "hyprial.shell.impl.gui.runtime" in primed
