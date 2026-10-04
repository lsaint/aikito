"""Opaque byte exchange and proof of non-delivery, independent of protocol."""

from collections.abc import Callable

Exchange = Callable[[bytes], bytes]


class TransportNotDelivered(Exception):
    """The transport proves the request bytes were never delivered."""


class TransportRejected(Exception):
    """A contracted endpoint rejected access before protocol dispatch.

    Only 401/403 carry this proof; neither response bodies nor credentials
    belong in this error. Retain pending even when dispatch was rejected.
    """

    def __init__(self, status: int) -> None:
        if type(status) is not int or status not in (401, 403):
            raise ValueError("Unsupported transport rejection status")
        self.status = status
        super().__init__(
            "Remote authentication required"
            if status == 401
            else "Remote authorization denied"
        )
