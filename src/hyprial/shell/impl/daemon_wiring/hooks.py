"""Named business hook consumers registered by the shell composition root."""


def hook_consumers():
    """H1 provides the registration seam; consumer migrations arrive later."""

    return {}


__all__ = ["hook_consumers"]
