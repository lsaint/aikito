"""Client-side RemoteStore adapter over a byte exchange.

The adapter holds only a ``bytes -> bytes`` transport boundary, never a backend
object. It encodes each operation, decodes the response, and validates the
response against the call's own context before trusting it.

Transport failure describes bytes delivery only. For a commit that may have
been delivered, any unconfirmable outcome becomes ``CommitOutcomeUnknown`` and
never a definite rejection; only ``TransportNotDelivered`` proves the request
did not reach the backend.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from types import MappingProxyType

from .payload import ResourcePayload, encode_payload, payload_hash
from .remote_transport import (
    Exchange as Exchange,
    TransportNotDelivered as TransportNotDelivered,
)
from .remote_protocol import (
    ERROR_EXCEPTIONS,
    Operation,
    ProtocolResponse,
    decode_response,
    encode_commit_request,
    encode_fetch_request,
    encode_read_request,
    encode_recover_request,
    encode_resolve_request,
)
from .remote_store import (
    CommitOutcomeUnknown,
    CommitRequest,
    CommitResult,
    InvalidContent,
    ProtocolError,
    RemoteSnapshot,
    StoreUnavailable,
)
from .remote_wire import validate_commit_result


class SerializedRemoteStore:
    """A ``RemoteStore`` implemented entirely through a byte exchange."""

    def __init__(self, exchange: Exchange) -> None:
        if not callable(exchange):
            raise TypeError("SerializedRemoteStore requires a byte exchange")
        self._exchange = exchange

    def validate_replica(self, local) -> None:
        """No-op: attachment constraints stay client-side and never serialize."""
        return

    def read(self) -> RemoteSnapshot:
        return self._perform(Operation.READ, encode_read_request()).body

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]:
        requested = tuple(dict.fromkeys(ids))
        response = self._perform(
            Operation.FETCH, encode_fetch_request(expected, requested)
        )
        payloads = response.body
        if set(payloads) != set(requested):
            raise ProtocolError("Fetch response does not match the requested resources")
        for identity, payload in payloads.items():
            descriptor = expected.resources.get(identity)
            if (
                descriptor is None
                or descriptor.content_hash != payload_hash(payload)
                or descriptor.size != len(encode_payload(payload))
            ):
                raise ProtocolError(
                    "Fetch payload does not match the expected snapshot"
                )
        return MappingProxyType(dict(payloads))

    def recover(self) -> bool:
        return self._perform(Operation.RECOVER, encode_recover_request()).body

    def commit(self, request: CommitRequest) -> CommitResult:
        try:
            message = encode_commit_request(request)
        except ProtocolError as exc:
            # A locally unencodable request never reaches the transport.
            raise InvalidContent("Commit request is not protocol-encodable") from exc
        result = self._perform(Operation.COMMIT, message, commit=True).body
        try:
            validate_commit_result(request, result)
        except InvalidContent as exc:
            raise CommitOutcomeUnknown("Commit result could not be confirmed") from exc
        return result

    def resolve_commit(
        self, sync_id: str, client_id: str, request_id: str, mutation_digest: str
    ) -> CommitResult | None:
        identity = (sync_id, client_id, request_id, mutation_digest)
        response = self._perform(
            Operation.RESOLVE_COMMIT, encode_resolve_request(*identity)
        )
        result = response.body
        if result is None:
            return None
        if (
            result.sync_id,
            result.client_id,
            result.request_id,
            result.mutation_digest,
        ) != identity:
            raise ProtocolError("Resolve receipt identity mismatch")
        return result

    def _perform(
        self, operation: str, message: bytes, *, commit: bool = False
    ) -> ProtocolResponse:
        try:
            reply = self._exchange(message)
        except TransportNotDelivered:
            raise StoreUnavailable(
                "Remote transport did not deliver the request"
            ) from None
        except Exception as exc:
            if commit:
                raise CommitOutcomeUnknown(
                    "Commit delivery could not be confirmed"
                ) from exc
            raise StoreUnavailable("Remote transport is unavailable") from exc
        try:
            response = decode_response(reply)
        except ProtocolError as exc:
            if commit:
                raise CommitOutcomeUnknown(
                    "Commit response could not be confirmed"
                ) from exc
            raise ProtocolError("Remote response is malformed") from exc
        if response.operation != operation:
            if commit:
                raise CommitOutcomeUnknown(
                    "Commit response operation mismatch"
                ) from None
            raise ProtocolError("Remote response operation mismatch")
        if not response.ok:
            raise ERROR_EXCEPTIONS[response.error_code](
                "Remote store rejected the operation"
            )
        return response
