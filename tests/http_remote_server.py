"""Test-only HTTP bridge with one-shot socket faults; not a hosted server."""

from __future__ import annotations

import argparse
import socket
import socketserver
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aikito.workspace import remote_limits
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_protocol import RemoteProtocolHandler, decode_request


class _FastThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class HTTPRemoteServer:
    def __init__(self, backend):
        self.handler = RemoteProtocolHandler(backend)
        self.requests: list[bytes] = []
        self.connections: list[tuple] = []
        self.headers: list[dict] = []
        self._faults: deque[tuple[str, str | None]] = deque()
        self._lock = threading.Lock()
        self.response_body: bytes | None = None
        self.delay = 0.1
        self._server = _FastThreadingHTTPServer(("127.0.0.1", 0), self._request_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.url = f"http://127.0.0.1:{self._server.server_port}/v1/remote"

    @property
    def request_count(self):
        with self._lock:
            return len(self.requests)

    def arm(self, fault: str, *, operation: str | None = None):
        with self._lock:
            self._faults.append((fault, operation))

    def _record(self, request, address, headers):
        with self._lock:
            self.requests.append(request)
            self.connections.append(address)
            self.headers.append(dict(headers))
            if self._faults:
                fault, operation = self._faults[0]
                if operation is None or decode_request(request).operation == operation:
                    self._faults.popleft()
                    return fault
        return None

    def _request_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    self._post()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def _post(self):
                self.connection.settimeout(5)
                if (
                    self.path != "/v1/remote"
                    or self.headers.get("Content-Type") != "application/octet-stream"
                ):
                    self.send_error(400)
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    self.send_error(400)
                    return
                try:
                    size = int(lengths[0])
                except ValueError:
                    self.send_error(400)
                    return
                if size < 0 or size > remote_limits.MAX_REMOTE_REQUEST_BYTES:
                    self.send_error(413)
                    return
                request = self.rfile.read(size)
                if len(request) != size:
                    return
                fault = owner._record(request, self.client_address, self.headers)
                if fault == "drop_after_request":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                response = owner.handler.handle(request)
                if owner.response_body is not None:
                    response = owner.response_body
                if fault == "drop_after_handler":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if fault == "delay_response":
                    # Interruptible so shutdown never waits for an injected delay.
                    owner._stopping.wait(owner.delay)
                status = (
                    500 if fault == "http_500" else 302 if fault == "redirect" else 200
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                if fault == "redirect":
                    self.send_header("Location", owner.url)
                if fault == "chunked_response":
                    self.send_header("Transfer-Encoding", "chunked")
                elif fault != "unframed_response":
                    length = len(response)
                    if fault == "truncate_response":
                        length += 10
                    elif fault == "oversized_response":
                        length = remote_limits.MAX_REMOTE_RESPONSE_BYTES + 1
                    self.send_header("Content-Length", str(length))
                self.end_headers()
                self.close_connection = True
                try:
                    if fault == "chunked_response":
                        self.wfile.write(
                            f"{len(response):x}\r\n".encode()
                            + response
                            + b"\r\n0\r\n\r\n"
                        )
                    else:
                        self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def __enter__(self):
        self._stopping = threading.Event()
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("HTTP test server did not stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    with HTTPRemoteServer(FilesystemRemote.create(args.root)) as server:
        print(server.url, flush=True)
        threading.Event().wait()


if __name__ == "__main__":
    main()
