"""Helpers for representing and parsing flash partition tables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_SIZE_TOKEN_RE = r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)[kKmMgG]?"
_ENV_REFERENCE_TEXT_RE = r"(?:\\)?\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*"
_MTDPARTS_SIZE_TEXT_RE = rf"(?:{_SIZE_TOKEN_RE}|-|{_ENV_REFERENCE_TEXT_RE})"
_MTDPARTS_ENTRY_TEXT_RE = (
    rf"{_MTDPARTS_SIZE_TEXT_RE}(?:@{_MTDPARTS_SIZE_TEXT_RE})?\([^)]+\)"
)
_MTDPARTS_ENTRY_RE = re.compile(
    rf"(?P<size>{_SIZE_TOKEN_RE}|-)(?:@(?P<offset>{_SIZE_TOKEN_RE}))?\((?P<name>[^)]+)\)"
)
_MTDPARTS_SPEC_RE = re.compile(
    rf"(?P<device>[A-Za-z0-9_.-]+):"
    rf"(?P<entries>{_MTDPARTS_ENTRY_TEXT_RE}(?:,{_MTDPARTS_ENTRY_TEXT_RE})*)"
)
_ENV_REFERENCE_RE = re.compile(
    r"(?:\\)?\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)


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
    """Return the mtdparts table embedded in an environment value.

    Environment values frequently contain a table after ``mtdparts=`` or a
    ``setenv mtdparts`` command, followed by unrelated boot arguments.  The
    match deliberately stops at the final contiguous partition entry.
    """
    for match in _MTDPARTS_SPEC_RE.finditer(value):
        return match.group(0)
    return None


def resolve_env_references(value: str, env: dict[str, str]) -> str:
    """Expand U-Boot-style references in *value* using *env*.

    Both ``${name}`` and ``$name`` are accepted.  Some compiled default
    environments preserve the former as ``\\${name}``; it has the same
    meaning here.  Missing variables and reference cycles are errors so a
    caller cannot accidentally parse a partially expanded flash layout.
    """
    resolved: dict[str, str] = {}
    resolving: set[str] = set()

    def expand(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("plain")
            assert name is not None
            return resolve(name)

        return _ENV_REFERENCE_RE.sub(replace, text)

    def resolve(name: str) -> str:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            raise ValueError(f"cyclic environment reference: {name}")
        try:
            raw = env[name]
        except KeyError as error:
            raise ValueError(f"undefined environment reference: {name}") from error
        resolving.add(name)
        try:
            result = expand(raw)
        finally:
            resolving.remove(name)
        resolved[name] = result
        return result

    return expand(value)


def parse_mtdparts_spec(
    spec: str,
    *,
    total_size: int | None = None,
) -> PartitionTable:
    text = spec.strip()
    match = _MTDPARTS_SPEC_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid mtdparts spec: {spec!r}")

    device = match.group("device")
    entries_data = list(_MTDPARTS_ENTRY_RE.finditer(match.group("entries")))

    entries: list[PartitionEntry] = []
    offset = 0
    open_ended_seen = False

    for index, entry_match in enumerate(entries_data):
        size_token = entry_match.group("size")
        name = entry_match.group("name").strip()
        if not name:
            raise ValueError(f"mtdparts spec contains an unnamed partition: {spec!r}")
        size = parse_size_token(size_token)
        offset_token = entry_match.group("offset")
        if offset_token is not None:
            offset = parse_size_token(offset_token)
            assert offset is not None
        if size is None:
            if index != len(entries_data) - 1:
                raise ValueError("open-ended partition must be the last entry")
            open_ended_seen = True
        elif open_ended_seen:
            raise ValueError("open-ended partition must be the last entry")
        entries.append(PartitionEntry(name=name, offset=offset, size=size))
        if size is not None:
            offset += size

    table = PartitionTable(device=device, entries=tuple(entries), total_size=total_size)
    if total_size is not None:
        for entry in table.resolved_entries():
            assert entry.size is not None
            if entry.offset + entry.size > total_size:
                raise ValueError(
                    f"partition {entry.name!r} exceeds total_size {total_size:#x}"
                )
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
