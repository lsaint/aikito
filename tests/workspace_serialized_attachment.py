"""Local filesystem attachment checks around a serialized store.

Only this same-machine test assembly knows the center path. The transport and
protocol handler never receive a replica path.
"""

from pathlib import Path

from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.serialized_remote import Exchange, SerializedRemoteStore


class FilesystemSerializedRemote(SerializedRemoteStore):
    def __init__(self, exchange: Exchange, center: Path):
        super().__init__(exchange)
        self._center = center

    def validate_replica(self, local: Path) -> None:
        FilesystemRemote(self._center).validate_replica(local)
