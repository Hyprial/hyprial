"""Child-only WhatsApp worker implementation."""

# The package intentionally exports nothing. Importing the native driver is
# reserved for ``python -m ...worker.tryx_driver`` in the supervised child.

__all__: list[str] = []
