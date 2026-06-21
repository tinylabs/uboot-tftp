"""Filename protocol parsing for U-Boot RRQ messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ClientMessage:
    """A parsed RRQ filename from the U-Boot client."""

    client_id: str
    segments: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        return self.segments[0] if self.segments else "bootstrap"


def normalize_client_id(value: str) -> str:
    client_id = value.lower()
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError(f"invalid client id: {value!r}")
    return client_id


def parse_client_filename(filename: str) -> ClientMessage:
    """Parse `id=<identifier>/...` RRQ filenames.

    Path segment values are URL-decoded. Segments in `key=value` form are also
    exposed in `values`.
    """

    path = filename.strip("/")
    if not path:
        raise ValueError("empty RRQ filename")

    raw_segments = tuple(unquote(segment) for segment in path.split("/") if segment)
    if not raw_segments:
        raise ValueError("empty RRQ filename")

    key, separator, value = raw_segments[0].partition("=")
    if key != "id" or separator != "=":
        raise ValueError("RRQ filename must start with id=<identifier>")

    client_id = normalize_client_id(value)

    message_segments = raw_segments[1:]
    values: dict[str, str] = {}
    for segment in message_segments:
        segment_key, segment_separator, segment_value = segment.partition("=")
        if segment_separator == "=" and segment_key:
            values[segment_key] = segment_value

    return ClientMessage(
        client_id=client_id,
        segments=message_segments,
        values=values,
    )
