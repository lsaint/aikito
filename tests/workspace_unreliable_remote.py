"""Script transport faults without clocks, sleeps or production-store internals.

Delayed requests retain canonical bytes until explicit delivery. Gates bound
thread waits and let tests order original/retry delivery or edits before a
response. Duplicate delivery crosses the backend's actual commit critical
section. New wrapper instances model client restart, not backend durability.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

from aikito.workspace.remote_store import CommitOutcomeUnknown, StoreUnavailable
from aikito.workspace.remote_wire import decode_commit_request, encode_commit_request


class Fault(Enum):
    UNAVAILABLE = "unavailable"
    LOSE_RESPONSE = "lose-response"
    DELAY_DELIVERY = "delay-delivery"
    DUPLICATE_DELIVERY = "duplicate-delivery"
    PAUSE_DELIVERY = "pause-delivery"
    PAUSE_RESPONSE = "pause-response"


class Gate:
    """Release one or more arrivals together; failures time out instead of hanging."""

    def __init__(self, arrivals=1):
        if type(arrivals) is not int or arrivals < 1:
            raise ValueError("Gate requires a positive arrival count")
        self._remaining = arrivals
        self._mutex = threading.Lock()
        self._ready = threading.Event()
        self._released = threading.Event()

    def arrive(self):
        with self._mutex:
            self._remaining -= 1
            if self._remaining == 0:
                self._ready.set()
        if not self._released.wait(10):
            raise AssertionError("Fault gate was not released")

    def wait(self):
        if not self._ready.wait(10):
            raise AssertionError("Fault gate did not receive all arrivals")

    def release(self):
        self._released.set()


class UnreliableRemote:
    """Portable store facade with one-shot faults and a byte-exact delivery audit."""

    def __init__(self, backend):
        self._backend = backend
        self._mutex = threading.Lock()
        self._faults = defaultdict(deque)
        self._delayed = deque()
        self._calls = []
        self._commits = []
        self._deliveries = []
        self._results = []

    def schedule(self, operation, fault, *, gate=None):
        if operation not in {"read", "fetch", "commit", "resolve_commit", "recover"}:
            raise ValueError("Unsupported fault operation")
        if not isinstance(fault, Fault) or (
            operation != "commit" and fault is not Fault.UNAVAILABLE
        ):
            raise ValueError("Unsupported transport fault")
        if (gate is not None) != (
            fault in {Fault.PAUSE_DELIVERY, Fault.PAUSE_RESPONSE}
        ):
            raise ValueError("Paused delivery or response requires a gate")
        with self._mutex:
            self._faults[operation].append((fault, gate))

    def _next(self, operation):
        with self._mutex:
            self._calls.append(operation)
            fault, gate = (
                self._faults[operation].popleft()
                if self._faults[operation]
                else (None, None)
            )
        if fault is Fault.UNAVAILABLE:
            raise StoreUnavailable(f"Injected {operation} unavailable")
        return fault, gate

    @property
    def calls(self):
        with self._mutex:
            return tuple(self._calls)

    @property
    def commits(self):
        with self._mutex:
            return tuple(self._commits)

    @property
    def deliveries(self):
        with self._mutex:
            return tuple(self._deliveries)

    @property
    def results(self):
        with self._mutex:
            return tuple(self._results)

    def validate_replica(self, local):
        self._backend.validate_replica(local)

    def read(self):
        self._next("read")
        return self._backend.read()

    def fetch(self, expected, ids):
        self._next("fetch")
        return self._backend.fetch(expected, ids)

    def recover(self):
        self._next("recover")
        return self._backend.recover()

    def resolve_commit(self, sync_id, client_id, request_id, mutation_digest):
        self._next("resolve_commit")
        return self._backend.resolve_commit(
            sync_id, client_id, request_id, mutation_digest
        )

    def _deliver(self, encoded, gate=None):
        if gate is not None:
            gate.arrive()
        with self._mutex:
            self._deliveries.append(encoded)
        result = self._backend.commit(decode_commit_request(encoded))
        with self._mutex:
            self._results.append(result)
        return result

    def deliver_delayed(self, *, gate=None):
        with self._mutex:
            if not self._delayed:
                raise AssertionError("No delayed request to deliver")
            encoded = self._delayed.popleft()
        return self._deliver(encoded, gate)

    def commit(self, request):
        encoded = encode_commit_request(request)
        with self._mutex:
            self._commits.append(encoded)
        fault, gate = self._next("commit")
        if fault is Fault.DELAY_DELIVERY:
            with self._mutex:
                self._delayed.append(encoded)
            raise CommitOutcomeUnknown("Injected queued request without response")
        if fault is Fault.DUPLICATE_DELIVERY:
            barrier = threading.Barrier(2)

            def duplicate():
                barrier.wait(timeout=10)
                return self._deliver(encoded)

            with ThreadPoolExecutor(max_workers=2) as executor:
                left, right = executor.submit(duplicate), executor.submit(duplicate)
                result = left.result(timeout=15)
                if right.result(timeout=15) != result:
                    raise AssertionError(
                        "Duplicate deliveries returned different receipts"
                    )
            return result
        result = self._deliver(encoded, gate if fault is Fault.PAUSE_DELIVERY else None)
        if fault is Fault.LOSE_RESPONSE:
            raise CommitOutcomeUnknown("Injected accepted response loss")
        if fault is Fault.PAUSE_RESPONSE:
            gate.arrive()
        return result
