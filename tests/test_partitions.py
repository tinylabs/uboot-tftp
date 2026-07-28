import pytest

from uboot_tftp.partitions import (
    PartitionEntry,
    extract_mtdparts_spec,
    parse_mtdparts_spec,
    replace_mtdparts_spec,
    resolve_env_references,
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


def test_extract_mtdparts_spec_stops_before_trailing_bootargs():
    spec = extract_mtdparts_spec(
        "bootargs=console=ttyS0 mtdparts=NOR_FLASH:256k(boot),64k(env),"
        "2048k(kernel),5120k(rootfs),-(rootfs_data) LX_MEM=0x200000"
    )

    assert spec == (
        "NOR_FLASH:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)"
    )


def test_replace_mtdparts_spec_handles_a_uboot_variable_reference():
    bootargs = "mem=${osmem} mtdparts=${mtdparts} ${extras}"

    updated = replace_mtdparts_spec(bootargs, "${_mtdparts}")

    assert updated == "mem=${osmem} mtdparts=${_mtdparts} ${extras}"


def test_resolve_env_references_handles_nested_and_escaped_uboot_references():
    resolved = resolve_env_references(
        r"mtdparts=nor:256k(boot),64k(env),\${kernel_size}(kernel),$rootfs_size(rootfs)",
        {
            "kernel_size": "${kernel_size_k}",
            "kernel_size_k": "2048k",
            "rootfs_size": "5120k",
        },
    )

    assert resolved == "mtdparts=nor:256k(boot),64k(env),2048k(kernel),5120k(rootfs)"


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({}, "undefined environment reference"),
        ({"one": "${two}", "two": "$one"}, "cyclic environment reference"),
    ],
)
def test_resolve_env_references_rejects_missing_and_cyclic_values(env, match):
    with pytest.raises(ValueError, match=match):
        resolve_env_references("${one}", env)


def test_parse_mtdparts_spec_rejects_non_tail_open_ended_partition():
    with pytest.raises(ValueError, match="last entry"):
        parse_mtdparts_spec("sfc:-(boot),64k(env)")


def test_parse_mtdparts_spec_rejects_malformed_or_oversized_tables():
    with pytest.raises(ValueError, match="invalid mtdparts"):
        parse_mtdparts_spec("sfc:64k(boot),not-a-partition")
    with pytest.raises(ValueError, match="exceeds total_size"):
        parse_mtdparts_spec("sfc:9m(boot)", total_size=8 * 2**20)
