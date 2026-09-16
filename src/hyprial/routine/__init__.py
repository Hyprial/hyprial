"""Self-drive routines: deterministic periodic duty cycles over PAC runs.

A routine is a DECLARED duty cycle (design-selfdrive-routine): on schedule it
queries a task source, routes each self-drivable task by a declarative
routing table, and produces one-shot PAC workflow runs.  The routine makes no
judgments — judgment lives in the kanban annotations; the routine only reads
labels and tracks.  No model turn ever happens here.
"""
