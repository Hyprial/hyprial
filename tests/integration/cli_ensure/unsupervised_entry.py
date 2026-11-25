"""The shipped CLI with the per-user service manager reported as unsupported.

Decision: daemon-first-z4-unsupervised-entry-20261117.  The service manager
cannot be isolated by HOME (KP-093) and ``daemon restart`` consults it, so on a
machine that has one the restart ends in ``SERVICE_SUPERVISION_LOST`` before
its unsupervised leg.  Like the lifecycle child entry, this replaces exactly
one seam before it runs the unmodified CLI: the platform answer of
``_service_manager``, which product code already handles as "no supervisor".
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.daemon import service


def _unsupported():
    raise service.ServicePlatformUnsupported("test: no per-user service manager")


service._service_manager = _unsupported

from hyprial.shell import main  # noqa: E402

if __name__ == "__main__":
    main()
