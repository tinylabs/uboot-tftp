"""Helpers for representing and parsing flash partition tables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_MTDPARTS_ENTRY_RE = re.compile(r"([0-9A-Fa-fx]+[kKmMgG]?|-)\(([^)]+)\)")


@dataclass(frozen=True)
class PartitionEntry:
    name: str
    offset: int
    size: int | None

    @property
    def end(self) -> int | None:
        return None if self.size is None else self.offset + self.size

    def range(self, *, total_size: int | None = None) -> tuple[int, int]:
        if self.size is not None:
            return self.offset, self.size
        if total_size is None:
            raise ValueError(f"partition {self.name!r} has open-ended size")
        if total_size < self.offset:
            raise ValueError(
                f"total_size {total_size:#x} is smaller than partition offset {self.offset:#x}"
            )
        return self.offset, total_size - self.offset


@dataclass(frozen=True)
class PartitionTable:
    device: str
    entries: tuple[PartitionEntry, ...]
    total_size: int | None = None

    def get(self, name: str) -> PartitionEntry | None:
        normalized = name.strip().lower()
        for entry in self.entries:
            if entry.name.lower() == normalized:
                return entry
        return None

    def require(self, name: str) -> PartitionEntry:
        entry = self.get(name)
        if entry is None:
            raise KeyError(name)
        return entry

    def range(self, name: str, *, total_size: int | None = None) -> tuple[int, int]:
        return self.require(name).range(total_size=self._resolve_total_size(total_size))

    def ranges(
        self,
        names: Iterable[str] | None = None,
        *,
        total_size: int | None = None,
    ) -> list[tuple[int, int]]:
        if names is None:
            entries = self.entries
        else:
            entries = tuple(self.require(name) for name in names)
        resolved_total_size = self._resolve_total_size(total_size)
        return [entry.range(total_size=resolved_total_size) for entry in entries]

    def resolved_entries(
        self,
        *,
        total_size: int | None = None,
    ) -> tuple[PartitionEntry, ...]:
        resolved_total_size = self._resolve_total_size(total_size)
        return tuple(
            PartitionEntry(name=entry.name, offset=offset, size=size)
            for entry in self.entries
            for offset, size in [entry.range(total_size=resolved_total_size)]
        )

    def with_total_size(self, total_size: int) -> "PartitionTable":
        if total_size < 0:
            raise ValueError("total_size must be non-negative")
        return PartitionTable(
            device=self.device,
            entries=self.entries,
            total_size=total_size,
        )

    def _resolve_total_size(self, total_size: int | None) -> int | None:
        return self.total_size if total_size is None else total_size


def extract_mtdparts_spec(value: str) -> str | None:
    for token in value.replace(";", " ").split():
        candidate = token.strip("\"'")
        if ":" not in candidate or "(" not in candidate or ")" not in candidate:
            continue
        if _MTDPARTS_ENTRY_RE.search(candidate) is None:
            continue
        return candidate
    return None


def parse_mtdparts_spec(
    spec: str,
    *,
    total_size: int | None = None,
) -> PartitionTable:
    text = spec.strip()
    device, separator, remainder = text.partition(":")
    if not separator or not device.strip():
        raise ValueError(f"invalid mtdparts spec: {spec!r}")

    entries_data = _MTDPARTS_ENTRY_RE.findall(remainder)
    if not entries_data:
        raise ValueError(f"mtdparts spec contains no partitions: {spec!r}")

    entries: list[PartitionEntry] = []
    offset = 0
    open_ended_seen = False

    for index, (size_token, raw_name) in enumerate(entries_data):
        name = raw_name.strip()
        if not name:
            raise ValueError(f"mtdparts spec contains an unnamed partition: {spec!r}")
        size = parse_size_token(size_token)
        if size is None:
            if index != len(entries_data) - 1:
                raise ValueError("open-ended partition must be the last entry")
            open_ended_seen = True
        elif open_ended_seen:
            raise ValueError("open-ended partition must be the last entry")
        entries.append(PartitionEntry(name=name, offset=offset, size=size))
        if size is not None:
            offset += size

    table = PartitionTable(device=device.strip(), entries=tuple(entries), total_size=total_size)
    if total_size is not None:
        table.resolved_entries()
    return table


def parse_size_token(token: str) -> int | None:
    token = token.strip().lower()
    if token == "-":
        return None

    multiplier = 1
    if token.endswith("k"):
        multiplier = 1024
        token = token[:-1]
    elif token.endswith("m"):
        multiplier = 1024 * 1024
        token = token[:-1]
    elif token.endswith("g"):
        multiplier = 1024 * 1024 * 1024
        token = token[:-1]

    base = 16 if token.startswith("0x") else 10
    return int(token, base) * multiplier
