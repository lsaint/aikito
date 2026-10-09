"""Local binding and access errors; never Remote Protocol error codes."""

from .remote_store import StoreError


class RemoteBindingError(StoreError):
    """The local binding or its credential reference cannot be used."""


class RemoteBindingExists(RemoteBindingError):
    """A remote binding already exists for this workspace."""


class RemoteBindingMissing(RemoteBindingError):
    """No remote binding exists for this workspace."""


class RemoteCredentialError(RemoteBindingError):
    """Remote credential is missing, invalid, or improperly referenced."""


class RemoteEndpointInsecure(RemoteBindingError):
    """Insecure remote endpoint; credentials require HTTPS outside loopback."""


class RemoteEndpointCredentialsForbidden(RemoteBindingError):
    """Credentials must not appear in endpoint URL query parameters."""


class RemoteAccessDenied(StoreError):
    """The endpoint rejected access before dispatch; retain any pending request."""

    def __init__(self, status: int) -> None:
        if type(status) is not int or status not in (401, 403):
            raise ValueError("Unsupported remote access status")
        self.status = status
        super().__init__(
            "Remote authentication required"
            if status == 401
            else "Remote authorization denied"
        )

