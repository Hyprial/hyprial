"""Internal immutable contracts shared by every chat adapter driver."""

from . import content as _content
from . import driver as _driver
from . import intents as _intents
from . import provenance as _provenance
from . import receipts as _receipts
from . import values as _values

__all__ = [
    *_values.__all__,
    *_content.__all__,
    *_intents.__all__,
    *_provenance.__all__,
    *_driver.__all__,
    *_receipts.__all__,
]

_EXPORTS = {
    **{name: _values for name in _values.__all__},
    **{name: _content for name in _content.__all__},
    **{name: _intents for name in _intents.__all__},
    **{name: _provenance for name in _provenance.__all__},
    **{name: _driver for name in _driver.__all__},
    **{name: _receipts for name in _receipts.__all__},
}


def __getattr__(name: str) -> object:
    try:
        provider = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    return getattr(provider, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
