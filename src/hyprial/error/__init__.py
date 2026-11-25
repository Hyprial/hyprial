# Decision (repository owner, 2026-11-19): the wire error vocabulary is not backend
# logic.  This root package sits outside the five deep modules
# (kernel/identity/daemon/biz/shell) and is importable from every layer without an
# import-ratchet exception.  It imports no other ``hyprial`` module.  Today it
# holds only ``codes``; exception types move here later, split by business area.
"""Wire error vocabulary shared by every layer (see ``hyprial.error.codes``)."""
