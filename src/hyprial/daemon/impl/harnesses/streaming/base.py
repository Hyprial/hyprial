"""Turn process family roots."""


from typing import TYPE_CHECKING



if TYPE_CHECKING:
    pass


class BaseTurnProcess:
    """Common process-family marker for shared turn lifecycle implementations.

    The sequential implementation below retains the established pump and all
    of its public behavior.  Concurrent implementations use this same family
    marker while owning a correlated multi-in-flight adapter.
    """

class ConcurrentTurnProcess(BaseTurnProcess):
    """Bounded concurrent turn-process family seam.

    Concrete adapters own their child wire, while this family-level contract
    validates the declared execution bound.  The Jev adapter supplies the
    complete process implementation and calls this initializer exactly once.
    """

    def __init__(self, *, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("concurrent turn-process capacity must be positive")
        self.concurrency = concurrency
