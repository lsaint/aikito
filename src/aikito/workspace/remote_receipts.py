"""Latest client receipts and predecessor checks shared by store implementations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .remote_store import (
    CommitResult,
    InvalidContent,
    ReceiptCursor,
    ReplicaHistoryMismatch,
    RequestIdentityMismatch,
    StoreIdentityMismatch,
)
from .remote_wire import decode_receipt, encode_receipt


@dataclass(frozen=True)
class ReceiptHistory:
    sync_id: str
    latest: Mapping[str, CommitResult] = field(default_factory=dict)

    def __post_init__(self):
        if any(
            not isinstance(receipt, CommitResult)
            or client_id != receipt.client_id
            or receipt.sync_id != self.sync_id
            for client_id, receipt in self.latest.items()
        ):
            raise InvalidContent("Invalid receipt identity in center state")
        object.__setattr__(self, "latest", MappingProxyType(dict(self.latest)))

    def resolve(
        self,
        sync_id: str,
        client_id: str,
        request_id: str,
        mutation_digest: str,
    ) -> CommitResult | None:
        ReceiptCursor(request_id, mutation_digest)
        if type(client_id) is not str or not client_id:
            raise InvalidContent("Invalid receipt client identity")
        if sync_id != self.sync_id:
            raise StoreIdentityMismatch("Resource center identity changed")
        receipt = self.latest.get(client_id)
        if receipt is None or receipt.request_id != request_id:
            return None
        if receipt.mutation_digest != mutation_digest:
            raise RequestIdentityMismatch("Request identity has a different digest")
        return receipt

    def check_previous(self, client_id: str, cursor: ReceiptCursor | None) -> None:
        receipt = self.latest.get(client_id)
        expected = (
            None
            if receipt is None
            else ReceiptCursor(
                receipt.request_id,
                receipt.mutation_digest,
            )
        )
        if cursor != expected:
            raise ReplicaHistoryMismatch(
                "Replica receipt history changed; preserve pending"
            )

    def accepted(self, receipt: CommitResult) -> ReceiptHistory:
        return ReceiptHistory(self.sync_id, {**self.latest, receipt.client_id: receipt})

    def encode(self) -> dict:
        return {
            "version": 1,
            "latest": {
                key: encode_receipt(value) for key, value in self.latest.items()
            },
        }

    @classmethod
    def decode(cls, sync_id: str, revision: int, raw: object) -> ReceiptHistory:
        if (
            type(raw) is not dict
            or set(raw) != {"version", "latest"}
            or type(raw["version"]) is not int
            or raw["version"] != 1
            or type(raw["latest"]) is not dict
        ):
            raise InvalidContent("Invalid receipt history version or fields")
        receipts = {key: decode_receipt(value) for key, value in raw["latest"].items()}
        if any(value.accepted_revision > revision for value in receipts.values()):
            raise InvalidContent("Receipt revision exceeds center revision")
        if len({value.accepted_revision for value in receipts.values()}) != len(
            receipts
        ):
            raise InvalidContent("Multiple clients have the same accepted revision")
        return cls(sync_id, receipts)
