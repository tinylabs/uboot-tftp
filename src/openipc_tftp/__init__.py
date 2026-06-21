"""Dynamic content helpers for tftpy-based TFTP servers."""

from .providers import CallableContentProvider, ContentRequest, ContentResult
from .protocol import ClientMessage, parse_client_filename
from .config import DaemonConfig, ScriptRoute, load_daemon_config
from .sessions import ClientSession, InMemorySessionStore, UBootAction
from .scripted import ClientHandle, ScriptedConfigProvider
from .uboot import UBootScriptProvider, UBootScriptRenderer
from .mkimage import extract_script_payload
from .uploads import DiskUploadStore, InMemoryUploadStore, UploadedFile, UploadRequest

__all__ = [
    "CallableContentProvider",
    "ClientMessage",
    "ClientSession",
    "ContentRequest",
    "ContentResult",
    "ClientHandle",
    "DaemonConfig",
    "InMemorySessionStore",
    "DynamicContentServer",
    "DiskUploadStore",
    "InMemoryUploadStore",
    "ScriptRoute",
    "ScriptedConfigProvider",
    "UploadedFile",
    "UploadRequest",
    "UBootAction",
    "UBootScriptProvider",
    "UBootScriptRenderer",
    "extract_script_payload",
    "load_daemon_config",
    "parse_client_filename",
]


def __getattr__(name: str):
    if name == "DynamicContentServer":
        from .server import DynamicContentServer

        return DynamicContentServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
