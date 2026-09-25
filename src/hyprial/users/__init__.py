"""Per-machine user store and user homes: one record per person.

See ``notes/design-user-home-2026-09-25.md``.  The store is node-local by
ruling (Allen, 2026-09-25: 「用户库每机一份」); nothing here replicates over
the mesh.
"""
