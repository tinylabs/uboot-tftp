"""Dynamic content helpers for tftpy-based TFTP servers."""

from .providers import CallableContentProvider, ContentRequest, ContentResult
from .protocol import ClientMessage, parse_client_filename
from .sessions import ClientSession, InMemorySessionStore, UBootAction
from .uboot import UBootScriptProvider, UBootScriptRenderer
from .mkimage import extract_script_payload
from .uploads import DiskUploadStore, InMemoryUploadStore, UploadedFile, UploadRequest

__all__ = [
    "CallableContentProvider",
    "ClientMessage",
    "ClientSession",
    "ContentRequest",
    "ContentResult",
    "InMemorySessionStore",
    "DynamicContentServer",
    "DiskUploadStore",
    "InMemoryUploadStore",
    "UploadedFile",
    "UploadRequest",
    "UBootAction",
    "UBootScriptProvider",
    "UBootScriptRenderer",
    "extract_script_payload",
    "parse_client_filename",
]


def __getattr__(name: str):
    if name == "DynamicContentServer":
        from .server import DynamicContentServer

        return DynamicContentServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
