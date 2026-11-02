"""CLI runtime: public composition ports plus the process entry.

``CliRuntime`` is the B/C registration and request boundary; ``entry`` is the
installed process entry.  Command families should import ``CliRuntime`` from
here (or from ``composition``) and must not import ``entry``.
"""

from hyprial.shell.impl.cli.runtime.composition import CliRuntime

__all__ = ["CliRuntime"]
