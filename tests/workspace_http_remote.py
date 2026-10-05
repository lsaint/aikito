"""HTTP test assembly and subprocess lifecycle for black-box contracts."""

from __future__ import annotations

import os
import json
import queue
import shlex
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aikito

from http_remote_server import HTTPRemoteServer

from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.serialized_remote import SerializedRemoteStore

TEST_TOKEN = "external-contract-test-token"


@contextmanager
def http_store(root):
    with HTTPRemoteServer(FilesystemRemote.create(root)) as server:
        yield SerializedRemoteStore(HTTPTransport(server.url).exchange)


@contextmanager
def external_endpoint(root, *, startup_timeout=30, token=None, config=None):
    """Read one endpoint line and always reap the independently started server.

    The command is an argv template, never a shell script. Format each token
    after splitting so a temporary root containing spaces remains one argument.
    """
    template = os.environ["AIKITO_REMOTE_TEST_SERVER_CMD"]
    if "{token}" in template and token is None:
        raise ValueError("External server command requires a test token")
    command = [
        part.replace("{root}", str(root))
        .replace("{token}", token or "")
        .replace("{config}", str(config or ""))
        for part in shlex.split(template)
    ]
    if not command or "{root}" not in template:
        raise ValueError("External server command must include {root}")
    root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    # Share the package under test, never another interpreter's standard library.
    package_root = str(Path(aikito.__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(
        [package_root, environment.get("PYTHONPATH", "")]
    )
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, env=environment
        )
        lines = queue.Queue()

        def read_endpoint():
            lines.put(process.stdout.readline(8192))
            # A long-running external server must not block on a full stdout pipe.
            while process.stdout.read(65536):
                pass

        reader = threading.Thread(target=read_endpoint, daemon=True)
        reader.start()
        try:
            try:
                line = lines.get(timeout=startup_timeout)
            except queue.Empty as exc:
                errors.seek(0)
                err_text = errors.read().decode("utf-8", "replace").strip()
                detail = f": {err_text}" if err_text else ""
                raise RuntimeError(
                    f"External server did not announce an endpoint{detail}"
                ) from exc
            if not line.endswith(b"\n"):
                errors.seek(0)
                err_text = errors.read().decode("utf-8", "replace").strip()
                detail = f": {err_text}" if err_text else ""
                raise RuntimeError(
                    f"External server did not announce an endpoint line{detail}"
                )
            url = line.decode("utf-8").strip()
            HTTPTransport(url)
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()


@contextmanager
def external_store(root, *, startup_timeout=30, token=None):
    if token is None and "{token}" in os.environ["AIKITO_REMOTE_TEST_SERVER_CMD"]:
        token = TEST_TOKEN
    with external_endpoint(root, startup_timeout=startup_timeout, token=token) as url:
        transport = HTTPTransport(
            url, authorization=None if token is None else "Bearer " + token
        )
        yield SerializedRemoteStore(transport.exchange)


class ExternalServer:
    """Restart one configured process without changing roots or endpoint identity.

    Test configuration is private to the harness. The first endpoint identifies
    the first configured store; all store routes share its listening address.
    """

    def __init__(self, root, stores):
        self.root = root
        self.stores = stores
        self.config = root.parent / (root.name + "-test-server.json")
        self.listen = "127.0.0.1:0"
        self._process = None

    def start(self):
        if self._process is not None:
            raise RuntimeError("External server is already running")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            json.dumps({"stores": self.stores, "listen": self.listen}),
            encoding="utf-8",
        )
        self._process = external_endpoint(self.root, config=self.config)
        try:
            self.url = self._process.__enter__()
        except BaseException:
            self._process = None
            raise
        parsed = urlsplit(self.url)
        self.listen = f"{parsed.hostname}:{parsed.port}"

    def stop(self):
        if self._process is not None:
            process, self._process = self._process, None
            process.__exit__(None, None, None)

    def restart(self):
        self.stop()
        self.start()

    def endpoint(self, store_id):
        parsed = urlsplit(self.url)
        return urlunsplit(parsed._replace(path=f"/v1/stores/{store_id}/remote"))

    def remote(self, store_id, token):
        return SerializedRemoteStore(
            HTTPTransport(
                self.endpoint(store_id),
                authorization=None if token is None else "Bearer " + token,
            ).exchange
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


def contract_backends(*local):
    return (
        ["external-http"]
        if os.environ.get("AIKITO_REMOTE_TEST_SERVER_CMD")
        else [*local, "http"]
    )
