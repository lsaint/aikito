"""Reject unsupported resource formats before interpreting logical IDs.

This is a client guard, not a storage or wire validation rule. A marker is
enough to stop this client; fetching an unsupported header cannot make it safe.
"""

import re

from .remote_store import RemoteSnapshot, StoreError

FORMAT_RESOURCE_ID = "format:encrypted-v1"
# Reserved namespaces: `format:` headers and `e<N>:` encrypted resource IDs.
# No business resource kind may use these prefixes, so any match fails closed
# with the same code regardless of the future format version.
_RESERVED_ID = re.compile(r"^(?:format:|e[0-9]+:)")


class EncryptedStoreUnsupported(StoreError):
    """The remote snapshot requires encrypted resource support."""

    code = "encrypted_store_unsupported"

    def __init__(self) -> None:
        super().__init__(
            f"{self.code}: Remote synchronization requires a supported encrypted Store. "
            "Initialize an empty Store or recover its device key before synchronizing."
        )


def require_plaintext_snapshot(snapshot: RemoteSnapshot) -> None:
    """Fail closed even when an encrypted snapshot has lost its format marker."""
    if any(_RESERVED_ID.match(identity) for identity in snapshot.resources):
        raise EncryptedStoreUnsupported()


def require_encrypted_snapshot(snapshot: RemoteSnapshot, *, allow_empty=False) -> None:
    """Only an empty Store may be bound before Vault initialization."""
    if allow_empty and not snapshot.resources:
        return
    if FORMAT_RESOURCE_ID not in snapshot.resources or any(
        identity != FORMAT_RESOURCE_ID
        and re.fullmatch(r"e1:[0-9a-f]{32}:[0-9a-f]{64}", identity) is None
        for identity in snapshot.resources
    ):
        raise EncryptedStoreUnsupported()
