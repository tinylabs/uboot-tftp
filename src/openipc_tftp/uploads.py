"""Upload capture support for tftpy WRQ/tftpput transfers."""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from .protocol import parse_client_filename


@dataclass(frozen=True)
class UploadRequest:
    filename: str
    peer: tuple[str, int]
    server_addr: tuple[str, int]


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    peer: tuple[str, int]
    server_addr: tuple[str, int]
    body: bytes
    created_at: float = field(default_factory=time.time)

    @property
    def size(self) -> int:
        return len(self.body)


class UploadStore(Protocol):
    def open(self, request: UploadRequest) -> BinaryIO:
        """Return a writable binary object for one upload."""


class CapturingUpload(io.BytesIO):
    def __init__(self, request: UploadRequest, store: InMemoryUploadStore) -> None:
        super().__init__()
        self._request = request
        self._store = store
        self._captured = False

    def close(self) -> None:
        if not self._captured:
            self._store.record(self._request, self.getvalue())
            self._captured = True
        super().close()


class InMemoryUploadStore:
    """Capture uploaded files in memory for protocol handling and debugging."""

    def __init__(self) -> None:
        self.uploads: list[UploadedFile] = []
        self.by_client_id: dict[str, list[UploadedFile]] = {}

    def open(self, request: UploadRequest) -> BinaryIO:
        return CapturingUpload(request, self)

    def record(self, request: UploadRequest, body: bytes) -> None:
        upload = UploadedFile(
            filename=request.filename,
            peer=request.peer,
            server_addr=request.server_addr,
            body=body,
        )
        self.uploads.append(upload)
        try:
            message = parse_client_filename(request.filename)
        except ValueError:
            return
        self.by_client_id.setdefault(message.client_id, []).append(upload)

    def all(self) -> list[UploadedFile]:
        return list(self.uploads)


class DiskUploadStore(InMemoryUploadStore):
    """Capture uploads in memory and persist them under an upload directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        super().__init__()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def record(self, request: UploadRequest, body: bytes) -> None:
        super().record(request, body)
        path = self.path_for(request.filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def path_for(self, filename: str) -> Path:
        try:
            message = parse_client_filename(filename)
        except ValueError:
            disk_filename = filename
        else:
            suffix = "/".join(message.segments)
            disk_filename = (
                message.client_id if not suffix else f"{message.client_id}/{suffix}"
            )

        relative = Path(disk_filename.lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe upload filename: {filename!r}")
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if root != path and root not in path.parents:
            raise ValueError(f"unsafe upload filename: {filename!r}")
        return path
