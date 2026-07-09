import pytest

from uboot_tftp.partitions import (
    PartitionEntry,
    extract_mtdparts_spec,
    parse_mtdparts_spec,
)


def test_parse_mtdparts_spec_builds_named_partition_table():
    table = parse_mtdparts_spec(
        "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)"
    )

    assert table.device == "sfc"
    assert table.entries == (
        PartitionEntry(name="boot", offset=0x00000, size=0x40000),
        PartitionEntry(name="env", offset=0x40000, size=0x10000),
        PartitionEntry(name="kernel", offset=0x50000, size=0x200000),
        PartitionEntry(name="rootfs", offset=0x250000, size=0x500000),
        PartitionEntry(name="rootfs_data", offset=0x750000, size=None),
    )


def test_partition_table_can_return_ranges_for_named_entries():
    table = parse_mtdparts_spec(
        "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        total_size=0x800000,
    )

    assert table.range("boot") == (0x00000, 0x40000)
    assert table.range("env") == (0x40000, 0x10000)
    assert table.range("rootfs_data") == (0x750000, 0x800000 - 0x750000)
    assert table.ranges(["kernel", "rootfs"]) == [
        (0x50000, 0x200000),
        (0x250000, 0x500000),
    ]


def test_partition_table_requires_total_size_for_open_ended_entries():
    table = parse_mtdparts_spec("sfc:256k(boot),-(rootfs_data)")

    with pytest.raises(ValueError, match="open-ended size"):
        table.range("rootfs_data")


def test_extract_mtdparts_spec_from_setenv_value():
    spec = extract_mtdparts_spec(
        "setenv mtdparts nand:256k(boot),768k(wtf),3072k(kernel),-(ubi)"
    )

    assert spec == "nand:256k(boot),768k(wtf),3072k(kernel),-(ubi)"


def test_parse_mtdparts_spec_rejects_non_tail_open_ended_partition():
    with pytest.raises(ValueError, match="last entry"):
        parse_mtdparts_spec("sfc:-(boot),64k(env)")
