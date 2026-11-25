"""Generic cell runtime: the OrgFS-layer connection structure (design §2, §9.3).

The package entry stays import-light on purpose: harness and turn code import
``cells.observability`` and must not load OrgFS or the runtime through it.
The public faces are ``cells.model``, ``cells.permissions``, ``cells.codec``
and ``cells.ports``; the runtime lives in ``cells.runtime`` and is composed by
``composition.cells``.
"""
