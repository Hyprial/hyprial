"""Human and machine views of an organization-context result.

``org context show``, ``org context status`` and ``org context fetch`` render
the same adopted document. Keeping the two view helpers here lets those
commands share one definition without growing ``commands.py`` past its
structural line budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import render_generic


def org_meta(document: Any) -> JsonObject:
    """The displayable fields the parsed body backs (never an adoption time).

    ``OrgMeta.to_json`` is the single recovery function; ``org status`` reads
    the same view from the parsed body, so show and status never disagree.
    """

    return dict(document.meta.to_json())


def org_human_view(data: Mapping[str, Any]) -> str:
    """The human view of an org result without the machine-only warnings.

    ``warnings`` is the ``--json`` home of the warning the ``warn`` channel
    already wrote to stderr; rendering it again on stdout would report the
    same fact twice in the human mode.
    """

    return render_generic(
        {key: value for key, value in data.items() if key != "warnings"}
    )
