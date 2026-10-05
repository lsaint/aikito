"""Portable multi-store and restart checks against one independent process."""

from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from workspace_store_setup import mutation, request, resolve

from aikito.workspace.remote_access import RemoteAccessDenied
from aikito.workspace.remote_store import RequestIdentityMismatch, SnapshotExpired

STORES = [
    {"store_id": "alpha", "token": "alpha-contract-token"},
    {"store_id": "beta", "token": "beta-contract-token"},
    {"store_id": "uninitialized", "token": "alpha-contract-token", "initialize": False},
]
TOKEN_A = STORES[0]["token"]
TOKEN_B = STORES[1]["token"]


def isolation(service):
    left = service.remote("alpha", TOKEN_A)
    right = service.remote("beta", TOKEN_B)
    initial_right = right.read()
    a = request(left, mutation("isolated-a"), client="same-client", identity="same-id")
    accepted_a = left.commit(a)
    assert right.read() == initial_right
    assert (
        right.resolve_commit(
            initial_right.sync_id, a.client_id, a.request_id, a.mutation_digest
        )
        is None
    )
    b = request(right, mutation("isolated-b"), client="same-client", identity="same-id")
    accepted_b = right.commit(b)
    assert accepted_a.sync_id != accepted_b.sync_id
    assert resolve(left, a) == accepted_a
    assert resolve(right, b) == accepted_b
    assert "memory:notes/isolated-a.md" not in right.read().resources
    assert "memory:notes/isolated-b.md" not in left.read().resources
    try:
        right.resolve_commit(
            accepted_b.sync_id, a.client_id, a.request_id, a.mutation_digest
        )
    except RequestIdentityMismatch:
        pass
    else:
        raise AssertionError("Cross-store request identity was accepted")


def access_rejections(service):
    left, right = service.remote("alpha", TOKEN_A), service.remote("beta", TOKEN_B)
    snapshots = left.read(), right.read()
    rejected = request(left, mutation("rejected"), client="denied-client")
    for store, token, status in [
        ("alpha", None, 401),
        ("alpha", "invalid-token", 401),
        ("alpha", TOKEN_B, 403),
        ("beta", TOKEN_A, 403),
        ("nonexistent", TOKEN_A, 403),
    ]:
        try:
            service.remote(store, token).commit(rejected)
        except RemoteAccessDenied as exc:
            assert exc.status == status
        else:
            raise AssertionError("Unauthorized commit was accepted")
    assert (left.read(), right.read()) == snapshots
    assert resolve(left, rejected) is None
    assert (
        right.resolve_commit(
            snapshots[1].sync_id,
            rejected.client_id,
            rejected.request_id,
            rejected.mutation_digest,
        )
        is None
    )
    # A partial body must not delay access rejection or leak store existence.
    url = urlsplit(service.endpoint("alpha"))
    for store, token, status in [
        ("alpha", "invalid-token", 401),
        ("beta", TOKEN_A, 403),
        ("nonexistent", TOKEN_A, 403),
        ("uninitialized", TOKEN_A, 404),
    ]:
        with socket.create_connection(
            (url.hostname, url.port), timeout=5
        ) as connection:
            connection.sendall(
                (
                    f"POST /v1/stores/{store}/remote HTTP/1.1\r\n"
                    f"Host: {url.netloc}\r\nAuthorization: Bearer {token}\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    "Content-Length: 1000000\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            first_line = connection.recv(4096).split(b"\r\n", 1)[0]
            assert int(first_line.split()[1]) == status
    assert (left.read(), right.read()) == snapshots


def restart(service):
    remote = service.remote("alpha", TOKEN_A)
    initial = remote.read()
    req = request(remote, mutation("restart"), client="restart-client")
    receipt = remote.commit(req)
    accepted = remote.read()
    before_url = service.endpoint("alpha")
    service.restart()
    assert service.endpoint("alpha") == before_url
    remote = service.remote("alpha", TOKEN_A)
    assert remote.read() == accepted
    assert remote.fetch(accepted, [req.mutations[0].id]) == {
        req.mutations[0].id: req.mutations[0].payload,
    }
    assert resolve(remote, req) == receipt
    assert remote.commit(req) == receipt
    assert remote.read() == accepted
    stale = request(remote, mutation("stale"), client="stale-client", expected=initial)
    try:
        remote.commit(stale)
    except SnapshotExpired:
        pass
    else:
        raise AssertionError("Restart invalidated CAS protection")
    assert remote.read() == accepted


def concurrent_cas(service):
    remote = service.remote("alpha", TOKEN_A)
    initial = remote.read()
    requests = [
        request(remote, mutation(f"race-{n}"), client=f"race-{n}", expected=initial)
        for n in range(2)
    ]

    ready = threading.Barrier(2)

    def submit(req):
        ready.wait(timeout=10)
        try:
            return remote.commit(req)
        except SnapshotExpired:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, requests))
    assert sum(result is not None for result in results) == 1
    assert remote.read().revision == initial.revision + 1
    for req, result in zip(requests, results):
        assert resolve(remote, req) == result
