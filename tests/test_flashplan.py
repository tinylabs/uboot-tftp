import asyncio
import zlib

import pytest

from uboot_tftp.flashplan import (
    PartitionPayload,
    build_partition_update_plan,
    partition_payload_crc32,
)


class FakeTftp:
    pass


def test_partition_payload_crc32_pads_with_erased_flash_bytes():
    payload = b"\x01\x02\x03"
    padded = payload + (b"\xFF" * 5)

    assert partition_payload_crc32(payload, size=8) == (
        zlib.crc32(padded) & 0xFFFFFFFF
    )


def test_partition_payload_crc32_rejects_oversized_payload():
    with pytest.raises(ValueError, match="exceeds partition size"):
        partition_payload_crc32(b"abcdef", size=4)


def test_build_partition_update_plan_batches_crc_requests(monkeypatch):
    import uboot_tftp.flashplan as flashplan

    calls: list[list[tuple[int, int]]] = []

    async def fake_uboot_crc32(tftp, ranges, **kwargs):  # noqa: ARG001
        calls.append(list(ranges))
        if len(calls) == 1:
            return [0] * len(ranges)
        return [1] * len(ranges)

    monkeypatch.setattr(flashplan, "uboot_crc32", fake_uboot_crc32)
    payloads = [
        PartitionPayload(
            name=f"part{index}",
            offset=index * 0x1000,
            size=0x1000,
            payload=bytes([index]),
            source=f"part{index}.bin",
        )
        for index in range(7)
    ]

    plan = asyncio.run(
        build_partition_update_plan(
            FakeTftp(),
            payloads,
            snapshot_base_addr=0x42000000,
        )
    )

    assert [len(call) for call in calls] == [6, 1]
    assert len(plan.updates) == 7


def test_build_partition_update_plan_marks_matching_and_changed_partitions(monkeypatch):
    import uboot_tftp.flashplan as flashplan

    payload_match = b"match"
    payload_change = b"change"

    async def fake_uboot_crc32(tftp, ranges, **kwargs):  # noqa: ARG001
        assert list(ranges) == [
            (0x42000000, 8),
            (0x42001000, 8),
        ]
        return [
            partition_payload_crc32(payload_match, size=8),
            0xDEADBEEF,
        ]

    monkeypatch.setattr(flashplan, "uboot_crc32", fake_uboot_crc32)

    plan = asyncio.run(
        build_partition_update_plan(
            FakeTftp(),
            [
                PartitionPayload(
                    name="kernel",
                    offset=0x0,
                    size=8,
                    payload=payload_match,
                    source="kernel.bin",
                ),
                PartitionPayload(
                    name="rootfs",
                    offset=0x1000,
                    size=8,
                    payload=payload_change,
                    source="rootfs.bin",
                ),
            ],
            snapshot_base_addr=0x42000000,
        )
    )

    assert [update.needs_update for update in plan.updates] == [False, True]
    assert [update.name for update in plan.pending()] == ["rootfs"]
