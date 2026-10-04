"""One opaque POST per fresh connection, with bounded response reads.

Only proven connection failures report non-delivery. Once connected, failures
remain ordinary exceptions so the caller can preserve an uncertain commit.
Timeouts bound idle socket operations, not the total exchange duration.
"""

from __future__ import annotations

import errno
import http.client
import math
import socket
import ssl
from urllib.parse import SplitResult, urlsplit

from . import remote_limits
from .remote_transport import TransportNotDelivered, TransportRejected

_LOCAL_SOCKET_ERRORS = {
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOBUFS,
    errno.ENOMEM,
    errno.EAFNOSUPPORT,
    errno.EPROTONOSUPPORT,
}


def validate_endpoint(url: str) -> SplitResult:
    """Validate endpoint syntax without including untrusted URL text in errors."""
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError("Invalid HTTP endpoint")
        endpoint = urlsplit(url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.fragment
        ):
            raise ValueError("Invalid HTTP endpoint")
        port = endpoint.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid HTTP endpoint port")
        path = endpoint.path or "/"
        if endpoint.query:
            path += "?" + endpoint.query
        path.encode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError("Invalid HTTP endpoint") from None
    return endpoint


class HTTPTransport:
    def __init__(
        self, url: str, *, timeout: float = 30.0, authorization: str | None = None
    ) -> None:
        endpoint = validate_endpoint(url)
        port = endpoint.port
        path = endpoint.path or "/"
        if endpoint.query:
            path += "?" + endpoint.query
        if authorization is not None and (
            type(authorization) is not str
            or not authorization
            or any(not 32 <= ord(char) <= 126 for char in authorization)
        ):
            raise ValueError("Invalid HTTP authorization header")
        self._authorization = authorization
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("HTTP timeout must be finite and positive")
        self._host = endpoint.hostname
        # An explicit default also prevents an unbracketed IPv6 host from being
        # interpreted as a host:port string by HTTPConnection.
        self._port = (
            port if port is not None else 443 if endpoint.scheme == "https" else 80
        )
        self._path = path
        self._secure = endpoint.scheme == "https"
        self._timeout = timeout

    def exchange(self, request: bytes) -> bytes:
        if type(request) is not bytes:
            raise TypeError("HTTP transport requires bytes")
        if len(request) > remote_limits.MAX_REMOTE_REQUEST_BYTES:
            raise ValueError("HTTP request exceeds the remote request size limit")
        connection_type = (
            http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        )
        connection = connection_type(self._host, self._port, timeout=self._timeout)
        # Global http.client debug settings must not print authorization values.
        connection.set_debuglevel(0)
        try:
            try:
                # Call the base implementation so TCP establishment and TLS have
                # separate failure boundaries even for HTTPSConnection.
                http.client.HTTPConnection.connect(connection)
            except OSError as exc:
                # Ambiguous connection failures stay conservative.
                if isinstance(exc, (socket.gaierror, ConnectionRefusedError)) or (
                    exc.errno in _LOCAL_SOCKET_ERRORS
                ):
                    raise TransportNotDelivered(
                        "HTTP connection was not established"
                    ) from exc
                raise
            if self._secure:
                context = ssl.create_default_context()
                context.set_alpn_protocols(["http/1.1"])
                connection.sock = context.wrap_socket(
                    connection.sock, server_hostname=self._host
                )
            headers = {
                "Content-Type": "application/octet-stream",
                "Connection": "close",
            }
            if self._authorization is not None:
                headers["Authorization"] = self._authorization
            connection.request(
                "POST",
                self._path,
                body=request,
                headers=headers,
            )
            with connection.getresponse() as response:
                if response.status in (401, 403):
                    raise TransportRejected(response.status)
                if response.status != 200:
                    raise ValueError(
                        f"HTTP transport received status {response.status}"
                    )
                return self._read_response(response)
        finally:
            connection.close()

    @staticmethod
    def _read_response(response: http.client.HTTPResponse) -> bytes:
        lengths = response.headers.get_all("Content-Length", [])
        transfers = response.headers.get_all("Transfer-Encoding", [])
        if len(lengths) > 1 or (lengths and transfers):
            raise ValueError("Ambiguous HTTP response framing")
        if transfers and (
            len(transfers) != 1 or transfers[0].lower().strip() != "chunked"
        ):
            raise ValueError("Unsupported HTTP response transfer encoding")
        declared = None
        if lengths:
            value = lengths[0].strip()
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Invalid HTTP response length")
            declared = int(value)
            if declared > remote_limits.MAX_REMOTE_RESPONSE_BYTES:
                raise ValueError("HTTP response exceeds the remote response size limit")
        chunks = []
        size = 0
        while True:
            chunk = response.read(
                min(64 * 1024, remote_limits.MAX_REMOTE_RESPONSE_BYTES - size + 1)
            )
            if not chunk:
                break
            size += len(chunk)
            if size > remote_limits.MAX_REMOTE_RESPONSE_BYTES:
                raise ValueError("HTTP response exceeds the remote response size limit")
            chunks.append(chunk)
        if declared is not None and size != declared:
            raise ValueError("Truncated HTTP response")
        return b"".join(chunks)
