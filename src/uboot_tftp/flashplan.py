"""Helpers for comparing candidate partition payloads against flash snapshots."""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .ubootops import uboot_crc32


@dataclass(frozen=True)
class PartitionPayload:
    name: str
    offset: int
    size: int
    payload: bytes
    source: str


@dataclass(frozen=True)
class PartitionDigest:
    name: str
    offset: int
    size: int
    flash_crc32: int
    payload_crc32: int


@dataclass(frozen=True)
class PartitionUpdate:
    name: str
    offset: int
    size: int
    payload: bytes
    source: str
    flash_crc32: int
    payload_crc32: int
    needs_update: bool


@dataclass(frozen=True)
class PartitionUpdatePlan:
    updates: tuple[PartitionUpdate, ...]

    def pending(self) -> tuple[PartitionUpdate, ...]:
        return tuple(update for update in self.updates if update.needs_update)


def partition_payload_crc32(payload: bytes, *, size: int) -> int:
    if size <= 0:
        raise ValueError("partition size must be positive")
    if len(payload) > size:
        raise ValueError(
            f"payload length {len(payload)} exceeds partition size {size}"
        )
    return zlib.crc32(payload + (b"\xFF" * (size - len(payload)))) & 0xFFFFFFFF


async def collect_partition_digests(
    tftp: Any,
    payloads: Iterable[PartitionPayload],
    *,
    snapshot_base_addr: int,
    little_endian: bool | None = None,
    key_prefix: str = "p",
) -> tuple[PartitionDigest, ...]:
    batch = tuple(payloads)
    if not batch:
        return ()

    payload_crc32 = [
        partition_payload_crc32(payload.payload, size=payload.size)
        for payload in batch
    ]
    flash_crc32: list[int] = []
    for index, chunk in enumerate(_chunked(batch, 6)):
        ranges = [
            (snapshot_base_addr + payload.offset, payload.size)
            for payload in chunk
        ]
        flash_crc32.extend(
            await uboot_crc32(
                tftp,
                ranges,
                little_endian=little_endian,
                key_prefix=f"{key_prefix}{index}_",
            )
        )

    return tuple(
        PartitionDigest(
            name=payload.name,
            offset=payload.offset,
            size=payload.size,
            flash_crc32=flash_crc32[index],
            payload_crc32=payload_crc32[index],
        )
        for index, payload in enumerate(batch)
    )


async def build_partition_update_plan(
    tftp: Any,
    payloads: Iterable[PartitionPayload],
    *,
    snapshot_base_addr: int,
    little_endian: bool | None = None,
    key_prefix: str = "p",
) -> PartitionUpdatePlan:
    batch = tuple(payloads)
    digests = await collect_partition_digests(
        tftp,
        batch,
        snapshot_base_addr=snapshot_base_addr,
        little_endian=little_endian,
        key_prefix=key_prefix,
    )
    return PartitionUpdatePlan(
        updates=tuple(
            PartitionUpdate(
                name=payload.name,
                offset=payload.offset,
                size=payload.size,
                payload=payload.payload,
                source=payload.source,
                flash_crc32=digest.flash_crc32,
                payload_crc32=digest.payload_crc32,
                needs_update=(digest.flash_crc32 != digest.payload_crc32),
            )
            for payload, digest in zip(batch, digests)
        )
    )


def _chunked(
    items: tuple[PartitionPayload, ...],
    size: int,
) -> Iterator[tuple[PartitionPayload, ...]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
