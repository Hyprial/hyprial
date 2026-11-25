"""Facade-side data that must live under ``impl`` to satisfy the boundary gate.

The public ``interface`` module keeps the export table and ``__getattr__``; only
the declaration-only imports live here.
"""
