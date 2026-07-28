import importlib.util
from pathlib import Path

import pytest


def load_openipc_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "openipc.py"
    spec = importlib.util.spec_from_file_location("openipc_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openipc_partition_table_prefers_size_specific_nor_layout():
    module = load_openipc_module()
    env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        "mtdpartsnor16m": (
            "setenv mtdparts "
            "sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)"
        ),
    }

    table = module.openipc_partition_table(env, flash_type="nor", flash_size=16 * 2**20)

    assert table.device == "sfc"
    assert table.range("kernel") == (0x50000, 0x300000)
    assert table.range("rootfs") == (0x350000, 0xA00000)


def test_openipc_partition_table_falls_back_to_default_mtdparts():
    module = load_openipc_module()
    env = {
        "mtdparts": "sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
    }

    table = module.openipc_partition_table(env, flash_size=8 * 2**20)

    assert table.range("boot") == (0x00000, 0x40000)
    assert table.range("env") == (0x40000, 0x10000)
    assert table.range("rootfs_data") == (0x750000, 0x800000 - 0x750000)


def test_openipc_partition_table_uses_matching_nor_layout_before_generic_or_nand():
    module = load_openipc_module()
    env = {
        "mtdpartsnor8m": "nor:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(data)",
        "mtdpartsnor16m": "nor:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(data)",
        "mtdparts": "nor:256k(boot),64k(env),1024k(kernel),2048k(rootfs),-(data)",
        "mtdpartsnand": "nand:256k(boot),64k(env),1024k(kernel),2048k(rootfs),-(ubi)",
    }

    table = module.openipc_partition_table(env, flash_type="nor", flash_size=16 * 2**20)

    assert table.range("kernel") == (0x50000, 0x300000)


def test_openipc_partition_table_chooses_smallest_layout_that_fits_payloads():
    module = load_openipc_module()
    env = {
        "mtdpartsnor8m": "nor:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(data)",
        "mtdpartsnor16m": "nor:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(data)",
    }

    small = module.openipc_partition_table(
        env,
        flash_type="nor",
        flash_size=16 * 2**20,
        payload_sizes={"uboot": 1, "kernel": 2 * 2**20, "rootfs": 5 * 2**20},
    )
    large = module.openipc_partition_table(
        env,
        flash_type="nor",
        flash_size=16 * 2**20,
        payload_sizes={"uboot": 1, "kernel": 2 * 2**20, "rootfs": 6 * 2**20},
    )

    assert small.range("rootfs") == (0x250000, 0x500000)
    assert large.range("rootfs") == (0x350000, 0xA00000)


def test_openipc_partition_table_reports_when_assets_do_not_fit_any_layout():
    module = load_openipc_module()
    env = {
        "mtdpartsnor8m": "nor:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(data)",
        "mtdpartsnor16m": "nor:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(data)",
    }

    with pytest.raises(ValueError, match="release assets do not fit"):
        module.openipc_partition_table(
            env,
            flash_type="nor",
            flash_size=16 * 2**20,
            payload_sizes={"uboot": 1, "kernel": 2 * 2**20, "rootfs": 11 * 2**20},
        )


def test_openipc_partition_table_resolves_embedded_bootargs_references():
    module = load_openipc_module()
    env = {
        "rootmtd": "5120k",
        "bootargs": (
            r"console=ttyS0 mtdparts=NOR_FLASH:256k(boot),64k(env),2048k(kernel),"
            r"\${rootmtd}(rootfs),-(rootfs_data) LX_MEM=\${unrelated_memory}"
        ),
    }

    table = module.openipc_partition_table(env, flash_type="nor", flash_size=8 * 2**20)

    assert table.range("rootfs") == (0x250000, 0x500000)


@pytest.mark.parametrize(
    "env",
    [
        {"mtdparts": "nor:256k(boot),64k(env),2048k(kernel),-(data)"},
        {"mtdparts": "nor:256k(boot),64k(env),2048k(kernel),$missing(rootfs)"},
        {"one": "${two}", "two": "${one}", "mtdparts": "nor:${one}(boot)"},
        {"mtdparts": "nor:256k(boot),64k(env),16m(kernel),1m(rootfs)"},
    ],
)
def test_openipc_partition_table_rejects_unusable_layouts(env):
    module = load_openipc_module()

    with pytest.raises(ValueError, match="unable to find"):
        module.openipc_partition_table(env, flash_type="nor", flash_size=8 * 2**20)
