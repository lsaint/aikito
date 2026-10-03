"""HTTP test assembly and subprocess lifecycle for black-box contracts."""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import aikito

from http_remote_server import HTTPRemoteServer

from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.serialized_remote import SerializedRemoteStore


@contextmanager
def http_store(root):
    with HTTPRemoteServer(FilesystemRemote.create(root)) as server:
        yield SerializedRemoteStore(HTTPTransport(server.url).exchange)


@contextmanager
def external_store(root, *, startup_timeout=30):
    """Read one endpoint line and always reap the independently started server.

    The command is an argv template, never a shell script. Format each token
    after splitting so a temporary root containing spaces remains one argument.
    """
    template = os.environ["AIKITO_REMOTE_TEST_SERVER_CMD"]
    command = [part.replace("{root}", str(root)) for part in shlex.split(template)]
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
            transport = HTTPTransport(url)
            yield SerializedRemoteStore(transport.exchange)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()


def contract_backends(*local):
    return (
        ["external-http"]
        if os.environ.get("AIKITO_REMOTE_TEST_SERVER_CMD")
        else [*local, "http"]
    )
