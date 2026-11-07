from __future__ import annotations

import ast
import inspect
from hyprial.daemon.impl.inbox import authority as authority_module

from ..support import (
    authority as authority,
)


def test_read_projection_source_has_no_sql_mutation_verbs() -> None:
    tree = ast.parse(inspect.getsource(authority_module.InboxReadProjection))
    strings = [
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any(
        verb in value
        for value in strings
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
    )
