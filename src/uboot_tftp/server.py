"""tftpy adapter for the minimal session/static model."""

from __future__ import annotations

import io
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from tftpy import TftpServer
from tftpy.TftpStates import TftpState, TftpServerState

from .protocol import parse_request_path
from .providers import ContentRequest, ContentResult, DynamicContentProvider
from .uploads import InMemoryUploadStore, UploadRequest

LOGGER = logging.getLogger(__name__)

_TFTPY_TIMEOUT_PATCHED = False


def _apply_tftpy_timeout_option_patch() -> None:
    global _TFTPY_TIMEOUT_PATCHED
    if _TFTPY_TIMEOUT_PATCHED:
        return

    original_return_supported = TftpState.returnSupportedOptions
    original_server_initial = TftpServerState.serverInitial

    def return_supported_options_with_timeout(self, options):
        passthrough_options = {
            key: value for key, value in options.items() if key != "timeout"
        }
        accepted = original_return_supported(self, passthrough_options)
        timeout_value = options.get("timeout")
        if timeout_value is None:
            return accepted
        try:
            parsed_timeout = int(timeout_value)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid TFTP timeout option %r", timeout_value)
            return accepted
        if parsed_timeout <= 0:
            LOGGER.warning("Ignoring non-positive TFTP timeout option %r", timeout_value)
            return accepted
        accepted["timeout"] = str(parsed_timeout)
        return accepted

    def server_initial_with_timeout(self, pkt, raddress, rport):
        sendoack = original_server_initial(self, pkt, raddress, rport)
        timeout_value = self.context.options.get("timeout")
        if timeout_value is None:
            return sendoack
        timeout_seconds = int(timeout_value)
        self.context.timeout = timeout_seconds
        self.context.sock.settimeout(timeout_seconds)
        return sendoack

    TftpState.returnSupportedOptions = return_supported_options_with_timeout
    TftpServerState.serverInitial = server_initial_with_timeout
    _TFTPY_TIMEOUT_PATCHED = True


_apply_tftpy_timeout_option_patch()


class _TransferLogFile:
    def __init__(
        self,
        fileobj: BinaryIO,
        *,
        action: str,
        filename: str,
        peer: tuple[str, int] | tuple[str, int, int, int],
        expected_size: int | None = None,
    ) -> None:
        self._fileobj = fileobj
        self._action = action
        self._filename = filename
        self._peer = peer
        self._expected_size = expected_size
        self._bytes = 0
        self._logged = False

    def read(self, size: int = -1):
        chunk = self._fileobj.read(size)
        self._bytes += _payload_length(chunk)
        return chunk

    def write(self, data):
        written = self._fileobj.write(data)
        self._bytes += written if isinstance(written, int) else _payload_length(data)
        return written

    def close(self) -> None:
        if not self._logged:
            total_bytes = self._expected_size if self._expected_size is not None else self._bytes
            LOGGER.info(
                "%s complete filename=%s peer=%s bytes=%s",
                self._action,
                self._filename,
                _format_peer(self._peer),
                total_bytes,
            )
            self._logged = True
        self._fileobj.close()

    def __enter__(self):
        self._fileobj.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._fileobj.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str):
        return getattr(self._fileobj, name)


class DynamicContentServer:
    """TFTP server with session-aware RRQ/WRQ handling."""

    def __init__(
        self,
        address: str,
        port: int,
        retries: int,
        timeout: int,
        provider: DynamicContentProvider,
        *,
        upload_store: InMemoryUploadStore,
        tftproot: str | os.PathLike[str],
        server_factory: Callable[..., TftpServer] = TftpServer,
    ) -> None:
        self.address = address
        self.port = port
        self.retries = retries
        self.timeout = timeout
        self.provider = provider
        self.upload_store = upload_store
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

    def reload(
        self,
        *,
        provider: DynamicContentProvider,
        upload_store: InMemoryUploadStore,
        tftproot: str | os.PathLike[str],
        address: str | None = None,
        port: int | None = None,
        retries: int | None = None,
        timeout: int | None = None,
    ) -> None:
        """Reload runtime configuration without replacing the listening socket."""

        new_root = str(tftproot)
        Path(new_root).mkdir(parents=True, exist_ok=True)
        if address is not None and address != self.address:
            LOGGER.warning(
                "Ignoring reloaded address change while server is running: %s -> %s",
                self.address,
                address,
            )
        if port is not None and port != self.port:
            LOGGER.warning(
                "Ignoring reloaded port change while server is running: %s -> %s",
                self.port,
                port,
            )
        if retries is not None:
            self.retries = int(retries)
        if timeout is not None:
            self.timeout = int(timeout)
        self.provider = provider
        self.upload_store = upload_store
        self.tftproot = new_root
        LOGGER.info("Reloaded server runtime state tftproot=%s", self.tftproot)

    def _open_dynamic_download(
        self,
        filename: str,
        *,
        raddress: str,
        rport: int,
    ) -> BinaryIO | None:
        LOGGER.info(
            "RRQ filename=%s peer=%s",
            filename,
            _format_peer((raddress, rport)),
        )
        request = ContentRequest(
            filename=filename,
            peer=(raddress, rport),
            server_addr=(self.address, self.port),
            options={},
        )
        if _is_null_filename(filename):
            result = ContentResult.from_bytes(b"")
            fileobj = fileobj_from_result(result)
            return _TransferLogFile(
                fileobj,
                action="RRQ",
                filename=filename,
                peer=(raddress, rport),
                expected_size=result.size,
            )
        try:
            result = self.provider.fetch(request)
        except FileNotFoundError:
            LOGGER.info(
                "RRQ miss filename=%s peer=%s",
                filename,
                _format_peer((raddress, rport)),
            )
            return None
        fileobj = fileobj_from_result(result)
        return _TransferLogFile(
            fileobj,
            action="RRQ",
            filename=filename,
            peer=(raddress, rport),
            expected_size=result.size,
        )

    def _open_upload(self, path: str, context) -> BinaryIO | None:
        context.flock = False
        filename = self._relative_path(path)
        request = UploadRequest(
            filename=filename,
            peer=(context.host, context.port),
            server_addr=(self.address, self.port),
        )
        parsed = parse_request_path(filename)
        LOGGER.info(
            "WRQ filename=%s peer=%s",
            filename,
            _format_peer((context.host, context.port)),
        )
        if _is_null_filename(filename):
            return _TransferLogFile(
                io.BytesIO(),
                action="WRQ",
                filename=filename,
                peer=(context.host, context.port),
            )
        if parsed.is_session:
            return _TransferLogFile(
                self.upload_store.open(request),
                action="WRQ",
                filename=filename,
                peer=(context.host, context.port),
            )
        disk_path = _resolve_disk_upload_path(Path(self.tftproot), filename)
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        return _TransferLogFile(
            disk_path.open("w+b"),
            action="WRQ",
            filename=filename,
            peer=(context.host, context.port),
        )

    def _relative_path(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.tftproot).replace(os.sep, "/")
        except ValueError:
            return path


def _resolve_disk_upload_path(root: Path, filename: str) -> Path:
    relative = Path(filename.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe upload filename: {filename!r}")
    candidate = (root / relative).resolve()
    root = root.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(f"unsafe upload filename: {filename!r}")
    return candidate


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


def _format_peer(peer: tuple[str, int] | tuple[str, int, int, int]) -> str:
    return f"{peer[0]}:{peer[1]}"


def _payload_length(payload: object) -> int:
    if payload is None:
        return 0
    try:
        return len(payload)
    except TypeError:
        return 0


def _is_null_filename(filename: str) -> bool:
    return filename.strip("/") == "_null"
