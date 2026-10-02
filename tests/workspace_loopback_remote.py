"""Test-only loopback transport: a real byte boundary to one backend.

This is not a latency simulator. It exists only to prove that reconciliation
and the RemoteStore contract can cross an actual encode/decode boundary where
the only thing exchanged is bytes.
"""

from __future__ import annotations

import json
from collections import deque

from aikito.workspace.remote_protocol import (
    Operation,
    RemoteProtocolHandler,
    encode_read_response,
)
from aikito.workspace.remote_store import RemoteSnapshot
from aikito.workspace.remote_transport import TransportNotDelivered


class LoopbackTransport:
    """Forward request bytes to a protocol handler and return response bytes."""

    def __init__(self, store):
        self._handler = RemoteProtocolHandler(store)
        self._injected: deque = deque()
        self.requests: list[bytes] = []
        self.responses: list[bytes] = []

    def exchange(self, request: bytes) -> bytes:
        if type(request) is not bytes:
            raise AssertionError("Loopback transport requires request bytes")
        self.requests.append(request)
        if self._injected:
            response = self._injected.popleft()
        else:
            response = self._handler.handle(request)
        if type(response) is not bytes:
            raise AssertionError("Loopback handler must return response bytes")
        self.responses.append(response)
        return response

    def inject(self, response: bytes) -> None:
        """Make the next exchange return this response instead of the backend's."""
        if type(response) is not bytes:
            raise AssertionError("Injected response must be bytes")
        self._injected.append(response)


class FaultyLoopback(LoopbackTransport):
    """Deliver (or withhold) one commit, then lose or corrupt its response.

    Disarmed by default so pairing and setup can run cleanly; call ``arm``
    immediately before the commit whose outcome should be uncertain.
    """

    def __init__(self, store, fault: str):
        super().__init__(store)
        self._fault = fault
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def exchange(self, request: bytes) -> bytes:
        if not (self._armed and self._is_commit(request)):
            return super().exchange(request)
        self._armed = False
        if self._fault == "not-delivered":
            raise TransportNotDelivered("simulated non-delivery")
        response = super().exchange(request)
        if self._fault == "lost":
            raise RuntimeError("simulated response loss")
        if self._fault == "corrupted":
            return b"garbage"
        if self._fault == "mismatch":
            return encode_read_response(RemoteSnapshot("mismatch", 0, {}))
        return response

    @staticmethod
    def _is_commit(request: bytes) -> bool:
        try:
            return json.loads(request).get("operation") == Operation.COMMIT
        except (ValueError, TypeError, AttributeError):
            return False
