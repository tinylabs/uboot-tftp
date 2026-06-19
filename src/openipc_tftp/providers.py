"""Provider interfaces for dynamic TFTP content."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Protocol


PeerAddress = tuple[str, int] | tuple[str, int, int, int]
ServerAddress = tuple[str, int] | tuple[str, int, int, int]


@dataclass(frozen=True)
class ContentRequest:
    """Information available when an RRQ filename is resolved."""

    filename: str
    peer: PeerAddress
    server_addr: ServerAddress
    options: Mapping[str, str]


@dataclass(frozen=True)
class ContentResult:
    """Content returned by a dynamic provider."""

    body: bytes | BinaryIO
    size: int | None = None
    close_body: bool = True

    @classmethod
    def from_bytes(cls, body: bytes) -> ContentResult:
        """Create a result from in-memory bytes."""

        return cls(body=body, size=len(body), close_body=False)

    @classmethod
    def from_stream(
        cls,
        body: BinaryIO,
        *,
        size: int | None = None,
        close_body: bool = True,
    ) -> ContentResult:
        """Create a result from a binary file-like object."""

        return cls(body=body, size=size, close_body=close_body)


class DynamicContentProvider(Protocol):
    """Protocol implemented by dynamic content backends."""

    def fetch(self, request: ContentRequest) -> ContentResult:
        """Return content for the requested filename.

        Raise FileNotFoundError when the requested filename cannot be resolved.
        Other exceptions are reported by the TFTP transport as transfer errors.
        """


class CallableContentProvider:
    """Adapter for simple function-based providers."""

    def __init__(self, fetch: Callable[[ContentRequest], ContentResult]) -> None:
        self._fetch = fetch

    def fetch(self, request: ContentRequest) -> ContentResult:
        return self._fetch(request)
