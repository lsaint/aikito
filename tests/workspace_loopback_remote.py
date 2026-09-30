"""Test-only loopback transport: a real byte boundary to one backend.

This is not a latency simulator. It exists only to prove that reconciliation
and the RemoteStore contract can cross an actual encode/decode boundary where
the only thing exchanged is bytes.
"""

from __future__ import annotations

from collections import deque

from aikito.workspace.remote_protocol import RemoteProtocolHandler


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
