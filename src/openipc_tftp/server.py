"""tftpy server adapter for dynamic TFTP content."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tftpy import TftpServer

from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .uploads import InMemoryUploadStore, UploadRequest, UploadStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferPeer:
    host: str
    port: int

    def as_tuple(self) -> tuple[str, int]:
        return (self.host, self.port)


class DynamicContentServer:
    """TFTP server that delegates RRQ and WRQ handling to package hooks."""

    def __init__(
        self,
        address: str,
        port: int,
        retries: int,
        timeout: int,
        provider: DynamicContentProvider,
        *,
        upload_store: UploadStore | None = None,
        tftproot: str | os.PathLike[str] | None = None,
        server_factory: Callable[..., TftpServer] = TftpServer,
    ) -> None:
        self.address = address
        self.port = port
        self.retries = retries
        self.timeout = timeout
        self.provider = provider
        self.upload_store = upload_store or InMemoryUploadStore()
        self._owns_tftproot = tftproot is None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        if tftproot is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="openipc-tftp-")
            self.tftproot = self._tempdir.name
        else:
            self.tftproot = str(tftproot)
            Path(self.tftproot).mkdir(parents=True, exist_ok=True)
        self._server = server_factory(
            tftproot=self.tftproot,
            dyn_file_func=self._open_dynamic_download,
            upload_open=self._open_upload,
        )

    def run(self, run_once: bool = False) -> None:
        if run_once:
            raise NotImplementedError("tftpy does not expose a run_once server mode")
        self._server.listen(
            listenip=self.address,
            listenport=self.port,
            timeout=self.timeout,
            retries=self.retries,
        )

    def close(self, now: bool = True) -> None:
        self._server.stop(now=now)
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def _open_dynamic_download(
        self,
        filename: str,
        *,
        raddress: str,
        rport: int,
    ) -> BinaryIO | None:
        request = ContentRequest(
            filename=filename,
            peer=(raddress, rport),
            server_addr=(self.address, self.port),
            options={},
        )
        try:
            result = self.provider.fetch(request)
        except FileNotFoundError:
            return None
        return fileobj_from_result(result)

    def _open_upload(self, path: str, context) -> BinaryIO | None:
        # tftpy 0.8.7 accepts a flock argument on TftpServer but does not pass
        # it through to TftpContextServer. Our upload sink is intentionally
        # file-like rather than an OS file, so disable advisory locking here.
        context.flock = False
        filename = self._relative_upload_filename(path)
        request = UploadRequest(
            filename=filename,
            peer=(context.host, context.port),
            server_addr=(self.address, self.port),
        )
        LOGGER.info(
            "Opening TFTP upload filename=%s peer=%s:%s",
            request.filename,
            context.host,
            context.port,
        )
        return self.upload_store.open(request)

    def _relative_upload_filename(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.tftproot).replace(os.sep, "/")
        except ValueError:
            return path


def fileobj_from_result(result: ContentResult) -> BinaryIO:
    fileobj = tempfile.TemporaryFile("w+b")
    if isinstance(result.body, bytes):
        fileobj.write(result.body)
    else:
        while chunk := result.body.read(1024 * 1024):
            fileobj.write(chunk)
        if result.close_body:
            result.body.close()
    fileobj.seek(0)
    return fileobj
