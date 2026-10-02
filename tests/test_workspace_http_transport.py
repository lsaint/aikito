"""Real socket failures, framing limits and opaque HTTP exchanges."""

from __future__ import annotations

import errno
import http.client
import socket
import ssl
from io import BytesIO
from email.message import Message

import pytest
from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote

from aikito.workspace import remote_limits
from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.remote_protocol import encode_read_request
from aikito.workspace.remote_store import CommitOutcomeUnknown, StoreUnavailable
from aikito.workspace.remote_transport import TransportNotDelivered
from aikito.workspace.serialized_remote import SerializedRemoteStore
from test_workspace_remote_receipts import request


@pytest.fixture
def server():
    with HTTPRemoteServer(InMemoryRemote("center")) as server:
        yield server


def test_opaque_post_and_fresh_connections(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    server.response_body = b"\x00opaque\xff"
    transport = HTTPTransport(server.url)
    for _ in range(2):
        assert transport.exchange(b"\x00request\xff") == server.response_body
    assert server.requests == [b"\x00request\xff"] * 2
    assert len(set(server.connections)) == 2
    assert all(h["Content-Type"] == "application/octet-stream" for h in server.headers)
    assert all(h["Connection"] == "close" for h in server.headers)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://host/x",
        "http:///x",
        "http://host:bad",
        "http://host:0",
        "http://host:65536",
        "http://user:pass@host",
        "http://host/x#fragment",
        "http://host/\n",
        "http://host/中文",
    ],
)
def test_invalid_endpoint_at_construction(url):
    with pytest.raises(ValueError):
        HTTPTransport(url)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        HTTPTransport("http://localhost", timeout=timeout)


def test_refused_socket_and_store_mapping():
    with HTTPRemoteServer(InMemoryRemote()) as server:
        url = server.url
    # Windows retries SYN to a closed local port before reporting refusal.
    transport = HTTPTransport(url, timeout=10)
    with pytest.raises(TransportNotDelivered):
        transport.exchange(b"request")
    remote = SerializedRemoteStore(transport.exchange)
    req = request(InMemoryRemote("center"))
    with pytest.raises(StoreUnavailable):
        remote.commit(req)


@pytest.mark.parametrize(
    "error", [socket.gaierror("DNS failed"), OSError(errno.EMFILE, "No sockets")]
)
def test_proven_connect_failures(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(socket, "create_connection", fail)
    with pytest.raises(TransportNotDelivered):
        HTTPTransport("http://localhost").exchange(b"request")


def test_tls_failure_after_tcp_is_ordinary(server, monkeypatch):
    def fail(context, *args, **kwargs):
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        raise ssl.SSLError("TLS failed")

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fail)
    with pytest.raises(ssl.SSLError):
        HTTPTransport(server.url.replace("http:", "https:")).exchange(b"request")


@pytest.mark.parametrize(
    "fault",
    [
        "drop_after_request",
        "drop_after_handler",
        "truncate_response",
        "delay_response",
        "http_500",
        "redirect",
        "oversized_response",
    ],
)
def test_socket_failures_are_not_non_delivery(server, fault):
    server.delay = 1
    server.arm(fault)
    with pytest.raises(Exception) as error:
        HTTPTransport(server.url, timeout=0.2).exchange(encode_read_request())
    assert not isinstance(error.value, TransportNotDelivered)
    assert server.request_count == 1


@pytest.mark.parametrize(
    "fault",
    [
        "drop_after_request",
        "drop_after_handler",
        "delay_response",
        "http_500",
        "redirect",
    ],
)
def test_commit_socket_failure_is_unknown(server, fault):
    server.delay = 1
    remote = SerializedRemoteStore(HTTPTransport(server.url, timeout=0.2).exchange)
    req = request(remote)
    server.arm(fault)
    with pytest.raises(CommitOutcomeUnknown):
        remote.commit(req)


@pytest.mark.parametrize("fault", ["chunked_response", "unframed_response"])
def test_bounded_streams_with_and_without_length(server, monkeypatch, fault):
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 128)
    server.response_body = b"x" * 128
    server.arm(fault)
    assert HTTPTransport(server.url).exchange(b"request") == b"x" * 128
    server.response_body += b"x"
    server.arm(fault)
    with pytest.raises(ValueError, match="size limit"):
        HTTPTransport(server.url).exchange(b"request")


def test_declared_length_limit_rejects_before_body_read(monkeypatch):
    class Response:
        headers = Message()

        def read(self, *args):
            raise AssertionError("Oversized declared body was read")

    Response.headers["Content-Length"] = "129"
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 128)
    with pytest.raises(ValueError, match="size limit"):
        HTTPTransport._read_response(Response())


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "-1")],
        [("Content-Length", "bad")],
        [("Content-Length", "1"), ("Content-Length", "1")],
        [("Content-Length", "1"), ("Transfer-Encoding", "chunked")],
        [("Transfer-Encoding", "gzip")],
    ],
)
def test_invalid_response_framing(headers):
    class Response:
        def __init__(self):
            self.headers = Message()
            for name, value in headers:
                self.headers[name] = value

        def read(self, *args):
            raise AssertionError("Invalid framing body was read")

    with pytest.raises(ValueError):
        HTTPTransport._read_response(Response())


def test_stream_limit_stops_at_one_excess_byte(monkeypatch):
    class Response(BytesIO):
        headers = Message()

    response = Response(b"x" * 1000)
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 128)
    with pytest.raises(ValueError, match="size limit"):
        HTTPTransport._read_response(response)
    assert response.tell() == 129


def test_request_limit_rejects_without_network(server, monkeypatch):
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 8)
    with pytest.raises(ValueError, match="size limit"):
        HTTPTransport(server.url).exchange(b"x" * 9)
    assert server.request_count == 0


def test_send_error_after_connect_is_not_non_delivery(server, monkeypatch):
    def fail(*args, **kwargs):
        raise ConnectionRefusedError("Send failed after connection")

    monkeypatch.setattr(http.client.HTTPConnection, "request", fail)
    with pytest.raises(ConnectionRefusedError):
        HTTPTransport(server.url).exchange(b"request")
