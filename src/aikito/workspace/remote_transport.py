"""Opaque byte exchange and proof of non-delivery, independent of protocol."""

from collections.abc import Callable

Exchange = Callable[[bytes], bytes]


class TransportNotDelivered(Exception):
    """The transport proves the request bytes were never delivered."""
